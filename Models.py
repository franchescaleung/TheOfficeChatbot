from vocab import *
from toTensor import *
import torch
from torch.jit import script, trace
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
import itertools
import random
import math, copy, sys
import nltk
nltk.download('wordnet')
from Dwight_Chat_transformer.MoveData import *
from Dwight_Chat_transformer.Transformer import *
from Dwight_Chat_transformer.TalkTrain import *


#===============================================================
#
# This file is where the magic happens. Here is the definitions of our models
# Note that there is an Encoder, Luong Attention decoder, greedy search decorders (softmax)
# and nucleus sampling based off of a linked git repo to the paper disussed in the report. 
# Lastly, this holds the code for the actual running of the model in Evaluate. 
#
#===============================================================

USE_CUDA = torch.cuda.is_available()
device = torch.device("cuda" if USE_CUDA else "cpu")
class EncoderRNN(nn.Module):
	def __init__(self, hidden_size, embedding, n_layers=1, dropout=0):
		super(EncoderRNN, self).__init__()
		# set at 2
		self.n_layers = n_layers
		# set at 500
		self.hidden_size = hidden_size
		# from torch.nn embeddings
		self.embedding = embedding

		# Initialize GRU; the input_size and hidden_size params are both set to 'hidden_size'
		#   because our input size is a word embedding with number of features == hidden_size
		self.gru = nn.GRU(hidden_size, hidden_size, n_layers,
						  dropout=(0 if n_layers == 1 else dropout), bidirectional=True)

	def forward(self, input_seq, input_lengths, hidden=None):
		# Convert word indexes to embeddings
		embedded = self.embedding(input_seq)
		# Pack padded batch of sequences for RNN module
		packed = nn.utils.rnn.pack_padded_sequence(embedded, input_lengths)
		# Forward pass through GRU
		outputs, hidden = self.gru(packed, hidden)
		# Unpack padding
		outputs, _ = nn.utils.rnn.pad_packed_sequence(outputs)
		# Sum bidirectional GRU outputs
		outputs = outputs[:, :, :self.hidden_size] + outputs[:, : ,self.hidden_size:]
		# Return output and final hidden state
		return outputs, hidden

# Luong attention layer
class Attn(nn.Module):
	def __init__(self, method, hidden_size):
		super(Attn, self).__init__()
		self.method = method
		if self.method not in ['dot', 'general', 'concat']:
			raise ValueError(self.method, "is not an appropriate attention method.")
		self.hidden_size = hidden_size
		if self.method == 'general':
			self.attn = nn.Linear(self.hidden_size, hidden_size)
		elif self.method == 'concat':
			self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
			self.v = nn.Parameter(torch.FloatTensor(hidden_size))

	def dot_score(self, hidden, encoder_output):
		return torch.sum(hidden * encoder_output, dim=2)

	def general_score(self, hidden, encoder_output):
		energy = self.attn(encoder_output)
		return torch.sum(hidden * energy, dim=2)

	def concat_score(self, hidden, encoder_output):
		energy = self.attn(torch.cat((hidden.expand(encoder_output.size(0), -1, -1), encoder_output), 2)).tanh()
		return torch.sum(self.v * energy, dim=2)

	def forward(self, hidden, encoder_outputs):
		# Calculate the attention weights (energies) based on the given method
		if self.method == 'general':
			attn_energies = self.general_score(hidden, encoder_outputs)
		elif self.method == 'concat':
			attn_energies = self.concat_score(hidden, encoder_outputs)
		elif self.method == 'dot':
			attn_energies = self.dot_score(hidden, encoder_outputs)

		# Transpose max_length and batch_size dimensions
		attn_energies = attn_energies.t()

		# Return the softmax normalized probability scores (with added dimension)
		return F.softmax(attn_energies, dim=1).unsqueeze(1)

class LuongAttnDecoderRNN(nn.Module):
	def __init__(self, attn_model, embedding, hidden_size, output_size, n_layers=1, dropout=0.1):
		super(LuongAttnDecoderRNN, self).__init__()

		# Keep for reference
		self.attn_model = attn_model
		self.hidden_size = hidden_size
		self.output_size = output_size
		self.n_layers = n_layers
		self.dropout = dropout

		# Define layers
		self.embedding = embedding
		self.embedding_dropout = nn.Dropout(dropout)
		self.gru = nn.GRU(hidden_size, hidden_size, n_layers, dropout=(0 if n_layers == 1 else dropout))
		self.concat = nn.Linear(hidden_size * 2, hidden_size)
		self.out = nn.Linear(hidden_size, output_size)

		self.attn = Attn(attn_model, hidden_size)

	def forward(self, input_step, last_hidden, encoder_outputs):
		# Note: we run this one step (word) at a time
		# Get embedding of current input word
		embedded = self.embedding(input_step)
		embedded = self.embedding_dropout(embedded)
		# Forward through unidirectional GRU
		rnn_output, hidden = self.gru(embedded, last_hidden)
		# Calculate attention weights from the current GRU output
		attn_weights = self.attn(rnn_output, encoder_outputs)
		# Multiply attention weights to encoder outputs to get new "weighted sum" context vector
		context = attn_weights.bmm(encoder_outputs.transpose(0, 1))
		# Concatenate weighted context vector and GRU output using Luong eq. 5
		rnn_output = rnn_output.squeeze(0)
		context = context.squeeze(1)
		concat_input = torch.cat((rnn_output, context), 1)
		concat_output = torch.tanh(self.concat(concat_input))
		# Predict next word using Luong eq. 6
		output = self.out(concat_output)
		output = F.softmax(output, dim=1)
		# Return output and final hidden state
		return output, hidden


def maskNLLLoss(inp, target, mask):
	nTotal = mask.sum()
	crossEntropy = -torch.log(torch.gather(inp, 1, target.view(-1, 1)).squeeze(1))
	loss = crossEntropy.masked_select(mask).mean()
	loss = loss.to(device)
	return loss, nTotal.item()

def nucleus(logits, top_p=0.0, filter_value=-float('Inf')):
	sorted_logits, sorted_indices = torch.sort(logits, descending=True)
	cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)
	
	# Remove tokens with cumulative probability above the threshold
	sorted_indices_to_remove = cumulative_probs > top_p
	# Shift the indices to the right to keep also the first token above the threshold
	sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
	sorted_indices_to_remove[..., 0] = 0

	indices_to_remove = sorted_indices[sorted_indices_to_remove]
	logits[indices_to_remove] = filter_value
	return logits


def train(input_variable, lengths, target_variable, mask, max_target_len, encoder, decoder, embedding, encoder_optimizer, decoder_optimizer, batch_size, clip, max_length=10):

	# Zero gradients
	encoder_optimizer.zero_grad()
	decoder_optimizer.zero_grad()

	# Set device options
	input_variable = input_variable.to(device)
	target_variable = target_variable.to(device)
	mask = mask.to(device)
	# Lengths for rnn packing should always be on the cpu
	lengths = lengths.to("cpu")

	# Initialize variables
	loss = 0
	print_losses = []
	n_totals = 0

	# Forward pass through encoder
	encoder_outputs, encoder_hidden = encoder(input_variable, lengths)

	# Create initial decoder input (start with SOS tokens for each sentence)
	decoder_input = torch.LongTensor([[SOS_token for _ in range(batch_size)]])
	decoder_input = decoder_input.to(device)

	# Set initial decoder hidden state to the encoder's final hidden state
	decoder_hidden = encoder_hidden[:decoder.n_layers]

	teacher_forcing_ratio = 1

	# Determine if we are using teacher forcing this iteration
	use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

	# Forward batch of sequences through decoder one time step at a time
	if use_teacher_forcing:
		for t in range(max_target_len):
			decoder_output, decoder_hidden = decoder(
				decoder_input, decoder_hidden, encoder_outputs
			)
			# Teacher forcing: next input is current target
			decoder_input = target_variable[t].view(1, -1)
			# Calculate and accumulate loss
			mask_loss, nTotal = maskNLLLoss(decoder_output, target_variable[t], mask[t])
			loss += mask_loss
			print_losses.append(mask_loss.item() * nTotal)
			n_totals += nTotal
	else:
		for t in range(max_target_len):
			decoder_output, decoder_hidden = decoder(
				decoder_input, decoder_hidden, encoder_outputs
			)
			# No teacher forcing: next input is decoder's own current output
			_, topi = decoder_output.topk(1)
			decoder_input = torch.LongTensor([[topi[i][0] for i in range(batch_size)]])
			decoder_input = decoder_input.to(device)
			# Calculate and accumulate loss
			mask_loss, nTotal = maskNLLLoss(decoder_output, target_variable[t], mask[t])
			loss += mask_loss
			print_losses.append(mask_loss.item() * nTotal)
			n_totals += nTotal

	# Perform backpropatation
	loss.backward()

	# Clip gradients: gradients are modified in place
	_ = nn.utils.clip_grad_norm_(encoder.parameters(), clip)
	_ = nn.utils.clip_grad_norm_(decoder.parameters(), clip)

	# Adjust model weights
	encoder_optimizer.step()
	decoder_optimizer.step()

	return sum(print_losses) / n_totals

def trainIters(model_name, voc, pairs, encoder, decoder, encoder_optimizer, decoder_optimizer, embedding, encoder_n_layers, decoder_n_layers, save_dir, n_iteration, batch_size, print_every, save_every, clip, corpus_name, loadFilename, checkpoint=None):

	# Load batches for each iteration
	training_batches = [batch2TrainData(voc, [random.choice(pairs) for _ in range(batch_size)])
					  for _ in range(n_iteration)]

	# Initializations
	print('Initializing ...')
	start_iteration = 1
	print_loss = 0
	if loadFilename:
		start_iteration = checkpoint['iteration'] + 1

	hidden_size = 500
	encoder_n_layers = 2
	decoder_n_layers = 2
	dropout = 0.1
	batch_size = 64
	# Training loop
	print("Training...")
	for iteration in range(start_iteration, n_iteration + 1):
		training_batch = training_batches[iteration - 1]
		# Extract fields from batch
		input_variable, lengths, target_variable, mask, max_target_len = training_batch

		# Run a training iteration with batch
		loss = train(input_variable, lengths, target_variable, mask, max_target_len, encoder,
					 decoder, embedding, encoder_optimizer, decoder_optimizer, batch_size, clip)
		print_loss += loss

		# Print progress
		if iteration % print_every == 0:
			print_loss_avg = print_loss / print_every
			print("Iteration: {}; Percent complete: {:.1f}%; Average loss: {:.4f}".format(iteration, iteration / n_iteration * 100, print_loss_avg))
			print_loss = 0

		# Save checkpoint
		if (iteration % save_every == 0):
			directory = os.path.join(save_dir, model_name, corpus_name, '{}-{}_{}'.format(encoder_n_layers, decoder_n_layers, hidden_size))
			if not os.path.exists(directory):
				os.makedirs(directory)
			torch.save({
				'iteration': iteration,
				'en': encoder.state_dict(),
				'de': decoder.state_dict(),
				'en_opt': encoder_optimizer.state_dict(),
				'de_opt': decoder_optimizer.state_dict(),
				'loss': loss,
				'voc_dict': voc.__dict__,
				'embedding': embedding.state_dict()
			}, os.path.join(directory, '{}_{}.tar'.format(iteration, 'checkpoint')))

class GreedySearchDecoder(nn.Module):
	def __init__(self, encoder, decoder):
		super(GreedySearchDecoder, self).__init__()
		self.encoder = encoder
		self.decoder = decoder

	def forward(self, input_seq, input_length, max_length):
		# Forward input through encoder model
		encoder_outputs, encoder_hidden = self.encoder(input_seq, input_length)
		# Prepare encoder's final hidden layer to be first hidden input to the decoder
		decoder_hidden = encoder_hidden[:self.decoder.n_layers]
		# Initialize decoder input with SOS_token
		decoder_input = torch.ones(1, 1, device=device, dtype=torch.long) * SOS_token
		# Initialize tensors to append decoded words to
		all_tokens = torch.zeros([0], device=device, dtype=torch.long)
		all_scores = torch.zeros([0], device=device)
		# Iteratively decode one word token at a time
		for _ in range(max_length):
			# Forward pass through decoder
			decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_outputs)
			# Obtain most likely word token and its softmax score
			decoder_scores, decoder_input = torch.max(decoder_output, dim=1)
			# Record token and score
			all_tokens = torch.cat((all_tokens, decoder_input), dim=0)
			all_scores = torch.cat((all_scores, decoder_scores), dim=0)
			# Prepare current token to be next decoder input (add a dimension)
			decoder_input = torch.unsqueeze(decoder_input, 0)
		# Return collections of word tokens and scores
		return all_tokens, all_scores

class nucleusSampling(nn.Module):
	def __init__(self, encoder, decoder):
		super(nucleusSampling, self).__init__()
		self.encoder = encoder
		self.decoder = decoder

	def forward(self, input_seq, input_length, max_length):
		# Forward input through encoder model
		encoder_outputs, encoder_hidden = self.encoder(input_seq, input_length)
		# Prepare encoder's final hidden layer to be first hidden input to the decoder
		decoder_hidden = encoder_hidden[:self.decoder.n_layers]
		# Initialize decoder input with SOS_token
		decoder_input = torch.ones(1, 1, device=device, dtype=torch.long) * SOS_token
		# Initialize tensors to append decoded words to
		all_tokens = torch.zeros([0], device=device, dtype=torch.long)
		all_scores = torch.zeros([0], device=device)
		# Iteratively decode one word token at a time
		for _ in range(max_length):
			# Forward pass through decoder
			decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_outputs)
			# decoder_output = decoder_output[0, -1, :]
			filtered_decoder_output = nucleus(decoder_output, .9)

			# Obtain most likely word token and its softmax score
			probabilities, decoder_input = torch.max(filtered_decoder_output, dim=-1)	
			# Record token and score
			all_tokens = torch.cat((all_tokens, decoder_input), dim=0)
			all_scores = torch.cat((all_scores, probabilities), dim=0)
			# Prepare current token to be next decoder input (add a dimension)
			decoder_input = torch.unsqueeze(decoder_input, 0)
		# Return collections of word tokens and scores
		return all_tokens, all_scores

def evaluate(encoder, decoder, searcher, voc, sentence, max_length=10):
	### Format input sentence as a batch
	# words -> indexes
	indexes_batch = [indexesFromSentence(voc, sentence)]
	# Create lengths tensor
	lengths = torch.tensor([len(indexes) for indexes in indexes_batch])
	# Transpose dimensions of batch to match models' expectations
	input_batch = torch.LongTensor(indexes_batch).transpose(0, 1)
	# Use appropriate device
	input_batch = input_batch.to(device)
	lengths = lengths.to(device)
	# Decode sentence with searcher
	tokens, scores = searcher(input_batch, lengths, max_length)
	# indexes -> words
	decoded_words = [voc.index2word[token.item()] for token in tokens]
	return decoded_words


opt = Options(batchsize=16, device=torch.device("cpu"), epochs=50, lr=0.01, max_len = 25, save_path = 'Dwight_Chat_transformer/saved/weights/transformer_custom_weights') #initialize our options for the chatbot
data_iter, infield, outfield, opt = json2datatools(path = 'Dwight_Chat_transformer/saved/custompairs.json', opt=opt) #make out infield/outfield vocabulary from our custom query/response pairings
emb_dim, n_layers, heads, dropout = 16, 8, 8, 0.1 #won't directly be used except in training, but needs to be defined for chatbot
dwight = Transformer(len(infield.vocab), len(outfield.vocab), emb_dim, n_layers, heads, dropout) #initialize the chatbot with its vocabulary
dwight.load_state_dict(torch.load(opt.save_path)) #load weights and options into chatbot

def evaluateInput(encoder, decoder, searcher, voc):
	input_sentence = ''
	samples = ["hello", "what is up", "bye"]
	for sample in samples:
		input_sentence = sample
		print("> ", input_sentence)
		# Check if it is quit case
		if input_sentence == 'q' or input_sentence == 'quit': break
		#get input from user
		dwight_reply = talk_to_chloe(input_sentence, dwight, opt, infield, outfield)
		# Normalize sentence
		input_sentence = normalizeString(input_sentence)
		# Evaluate sentence
		output_words = evaluate(encoder, decoder, searcher, voc, input_sentence)
		# Format and print response sentence
		output_words[:] = [x for x in output_words if not (x == 'EOS' or x == 'PAD')]
		print('RNNDwightBot:', ' '.join(output_words))
		print('TransformerDwightBot: '+ dwight_reply + '\n')

	while(1):
		try:
			# Get input sentence
			input_sentence = input('> ')
			# Check if it is quit case
			if input_sentence == 'q' or input_sentence == 'quit': break
			#get input from user
			dwight_reply = talk_to_chloe(input_sentence, dwight, opt, infield, outfield)
			# Normalize sentence
			input_sentence = normalizeString(input_sentence)
			# Evaluate sentence
			output_words = evaluate(encoder, decoder, searcher, voc, input_sentence)
			# Format and print response sentence
			output_words[:] = [x for x in output_words if not (x == 'EOS' or x == 'PAD')]
			print('RNNDwightBot:', ' '.join(output_words))
			print('TransformerDwightBot: '+ dwight_reply + '\n')

		except KeyError:
			print("Error: Encountered unknown word.")

