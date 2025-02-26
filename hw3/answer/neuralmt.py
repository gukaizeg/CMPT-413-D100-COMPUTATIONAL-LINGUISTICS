# -*- coding: utf-8 -*-
# Python version: 3.8+
#
# SFU CMPT413/825 Fall 2023, HW4
# default solution
# Simon Fraser University
# Jetic Gū
#
#
import os
import re
import sys
import optparse
from tqdm import tqdm

import spacy

import torch
from torch import nn
from torchtext.vocab import build_vocab_from_iterator
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader


class hp:
    pad_idx = 0
    sos_idx = 1
    eos_idx = 2
    unk_idx = 3
    lex_min_freq = 1

    # architecture
    hidden_dim = 256
    embed_dim = 256
    n_layers = 2
    dropout = 0.2
    batch_size = 32
    num_epochs = 10
    lexicon_cap = 25000

    # training
    max_lr = 1e-4
    cycle_length = 3000

    # generation
    max_len = 50

    # system
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ---YOUR ASSIGNMENT---
# -- Step 1: Baseline ---
# The attention module is completely broken now. Fix it using the definition
# given in the HW description.
class AttentionModule(nn.Module):
    def __init__(self, attention_dim):
        super(AttentionModule, self).__init__()
        self.W_enc = nn.Linear(attention_dim, attention_dim, bias=False)
        self.W_dec = nn.Linear(attention_dim, attention_dim, bias=False)
        self.V_att = nn.Linear(attention_dim, 1, bias=False)

    def calcAlpha(self, decoder_hidden, encoder_out):
        seq_len, batch_size, _ = encoder_out.shape
        encoder_out = encoder_out.permute(1, 0, 2)  # (batch, seq_len, dim)
        decoder_hidden_exp = decoder_hidden.repeat(1, seq_len, 1)  # (batch, seq_len, dim)
        scores = self.V_att(torch.tanh(self.W_enc(encoder_out) + self.W_dec(decoder_hidden_exp))).squeeze(-1)  # (batch, seq_len)
        alpha = torch.nn.functional.softmax(scores, dim=1)  # (batch, seq_len)
        return alpha 

    def forward(self, decoder_hidden, encoder_out):
        # (batch, dim)
        alpha = self.calcAlpha(decoder_hidden, encoder_out)  # (batch, seq_len)
        alpha = alpha.unsqueeze(1)  # (batch, 1, seq_len)
        encoder_out = encoder_out.permute(1, 0, 2)  # (batch, seq_len, dim)
        context = torch.bmm(alpha, encoder_out)  # (batch, 1, dim)
        context = context.permute(1, 0, 2)  # (1, batch, dim) 
        alpha = alpha.squeeze(1)
        return context, alpha


# -- Step 2: Improvements ---
# Implement UNK replacement, BeamSearch, translation termination criteria here,
# you can change 'greedyDecoder' and 'translate'.
def greedyDecoder(decoder, encoder_out, encoder_hidden, maxLen):
    seq1_len, batch_size, _ = encoder_out.size()
    target_vocab_size = decoder.target_vocab_size

    outputs = torch.autograd.Variable(
        encoder_out.data.new(maxLen, batch_size, target_vocab_size))
    alphas = torch.zeros(maxLen, batch_size, seq1_len)
    # take what we need from encoder
    decoder_hidden = encoder_hidden[-decoder.n_layers:]
    # start token (ugly hack)
    output = torch.autograd.Variable(
        outputs.data.new(1, batch_size).fill_(hp.sos_idx).long())
    for t in range(maxLen):
        output, decoder_hidden, alpha = decoder(
            output, encoder_out, decoder_hidden)
        outputs[t] = output
        alphas[t] = alpha.data
        output = torch.autograd.Variable(output.data.max(dim=2)[1])
        if int(output.data) == hp.eos_idx:
            break
    return outputs, alphas.permute(1, 2, 0)

def remove_adjacent_duplicates(text):
    words = text.split()
    result = [words[i] for i in range(len(words)) if i == 0 or words[i] != words[i - 1]]
    return ' '.join(result)

def translate(model, input_dl):
    results = []
    for i, batch in tqdm(enumerate(input_dl)):
        f, e = batch
        output, attention = model(f)
        output = output.topk(1)[1]
        output = model.tgt2txt(output[:, 0].data).strip().split('<eos>')[0]
        #output = remove_adjacent_duplicates(output)
        results.append(output)
    return results


# ---Model Definition etc.---
# DO NOT MODIFY ANYTHING BELOW HERE


class Encoder(nn.Module):
    """
    Encoder class
    """
    def __init__(self, source_vocab_size, embed_dim, hidden_dim,
                 n_layers, dropout):
        super(Encoder, self).__init__()
        self.hidden_dim = hidden_dim
        self.embed = nn.Embedding(source_vocab_size, embed_dim,
                                  padding_idx=hp.pad_idx)
        self.rnn = nn.GRU(embed_dim,
                          hidden_dim,
                          n_layers,
                          dropout=dropout,
                          bidirectional=True)

    def forward(self, source, hidden=None):
        """
        param source: batched input indices
        param hidden: initial hidden value of self.rnn
        output (encoder_out, encoder_hidden):
            encoder_hidden: the encoder RNN states of length len(source)
            encoder_out: the final encoder states, both direction summed up
                together h^{forward} + h^{backward}
        """
        embedded = self.embed(source)  # (batch_size, seq_len, embed_dim)
        # get encoded states (encoder_hidden)
        encoder_out, encoder_hidden = self.rnn(embedded, hidden)

        # sum bidirectional outputs
        encoder_final = (encoder_out[:, :, :self.hidden_dim] +  # forward
                         encoder_out[:, :, self.hidden_dim:])   # backward

        # encoder_final:  (seq_len, batch_size, hidden_dim)
        # encoder_hidden: (n_layers * num_directions, batch_size, hidden_dim)
        return encoder_final, encoder_hidden


class Decoder(nn.Module):
    def __init__(self, target_vocab_size,
                 embed_dim, hidden_dim,
                 n_layers,
                 dropout):
        super(Decoder, self).__init__()
        self.target_vocab_size = target_vocab_size
        self.n_layers = n_layers
        self.embed = nn.Embedding(target_vocab_size,
                                  embed_dim,
                                  padding_idx=hp.pad_idx)
        self.attention = AttentionModule(hidden_dim)

        self.rnn = nn.GRU(embed_dim + hidden_dim,
                          hidden_dim,
                          n_layers,
                          dropout=dropout)

        self.out = nn.Linear(hidden_dim * 2, target_vocab_size)

    def forward(self, output, encoder_out, decoder_hidden):
        """
        decodes one output frame
        """
        embedded = self.embed(output)  # (1, batch, embed_dim)
        context, alpha = self.attention(decoder_hidden[-1:], encoder_out)
        # 1, 1, 50 (seq, batch, hidden_dim)
        rnn_output, decoder_hidden =\
            self.rnn(torch.cat([embedded, context], dim=2), decoder_hidden)
        output = self.out(torch.cat([rnn_output, context], 2))
        return output, decoder_hidden, alpha


class Seq2Seq(nn.Module):
    def __init__(self, srcLex=None, tgtLex=None, build=True):
        super(Seq2Seq, self).__init__()
        # If we are loading the model, we don't build it here
        if build is True:
            self.params = {
                'srcLex': srcLex,
                'tgtLex': tgtLex,
                'srcLexSize': len(srcLex.vocab),
                'tgtLexSize': len(tgtLex.vocab),
                'embed_dim': hp.embed_dim,
                'hidden_dim': hp.hidden_dim,
                'n_layers': hp.n_layers,
                'dropout': hp.dropout,
                'maxLen': hp.max_len,
            }
            self.build()

    def build(self):
        # self.params are loaded, start building the model accordingly
        self.encoder = Encoder(
            source_vocab_size=self.params['srcLexSize'],
            embed_dim=self.params['embed_dim'],
            hidden_dim=self.params['hidden_dim'],
            n_layers=self.params['n_layers'],
            dropout=self.params['dropout'])
        self.decoder = Decoder(
            target_vocab_size=self.params['tgtLexSize'],
            embed_dim=self.params['embed_dim'],
            hidden_dim=self.params['hidden_dim'],
            n_layers=self.params['n_layers'],
            dropout=self.params['dropout'])
        self.maxLen = self.params['maxLen']

    def forward(self, source, maxLen=None):
        """
        This method implements greedy decoding
        param source: batched input indices
        param maxLen: maximum length of generated output
        """
        if maxLen is None:
            maxLen = self.maxLen
        encoder_out, encoder_hidden = self.encoder(source)

        return greedyDecoder(self.decoder, encoder_out, encoder_hidden,
                             maxLen)

    def tgt2txt(self, tgt):
        return " ".join([self.params['tgtLex'].get_itos()[int(i)] for i in tgt])

    def save(self, file):
        torch.save((self.params, self.state_dict()), file)

    def load(self, file):
        self.params, state_dict = torch.load(file, map_location='cpu')
        self.build()
        self.load_state_dict(state_dict)

# Load Tokeniser
token_en = spacy.load("en_core_web_sm") # Load the English model to tokenize English text
token_de = spacy.load("de_core_news_sm") # Load the German model to tokenize German text


def tokenise_en(text):
    """
    Tokenize an English text and return a list of tokens
    """
    return [token.text for token in token_en.tokenizer(text)]


def tokenise_de(text):
    """
    Tokenize a German text and return a list of tokens
    """
    return [token.text for token in token_de.tokenizer(text)]


def nl_load(inFile, linesToLoad=sys.maxsize, tokeniser=None):
    if tokeniser is not None:
        return [tokeniser(e.lower().strip()) for e in open(inFile, 'r')][:linesToLoad]
    else:
        return [e.lower().strip().split() for e in open(inFile, 'r')][:linesToLoad]


class Dataset(torch.utils.data.Dataset):
    def __init__(self, src="../data/train.tok.de", tgt="../data/train.tok.en",
                 srcLex=None, tgtLex=None, linesToLoad=sys.maxsize) -> None:
        self.source = nl_load(src, linesToLoad, tokeniser=tokenise_de)
        self.target = nl_load(tgt, linesToLoad, tokeniser=tokenise_en)
        self.srcLex = srcLex
        self.tgtLex = tgtLex
        return

    def __getitem__(self, idx) -> torch.Tensor:
        # load one sample by index, e.g like this:
        source_sample = self.source[idx]
        target_sample = self.target[idx]
        return source_sample, target_sample

    def __len__(self):
        return len(self.source)

    def build_vocab(self):
        """
        Construct vocabulary for both src and tgt using loaded data, returns said
        lex
        """
        def get_tokens(data_iter, place):
            for de, en in data_iter:
                if place == 0:
                    yield de
                else:
                    yield en
    
        self.srcLex = build_vocab_from_iterator(
            get_tokens(self, 0),
            min_freq = hp.lex_min_freq,
            specials = ['<pad>', '<sos>', '<eos>', '<unk>'],
            special_first=True
        )
        self.srcLex.set_default_index(self.srcLex['<unk>'])
        
        self.tgtLex = build_vocab_from_iterator(
            get_tokens(self, 1),
            min_freq = hp.lex_min_freq,
            specials = ['<pad>', '<sos>', '<eos>', '<unk>'],
            special_first=True
        )
        self.tgtLex.set_default_index(self.tgtLex['<unk>'])
        assert self.srcLex['<pad>'] == self.tgtLex['<pad>'] == hp.pad_idx
        assert self.srcLex['<sos>'] == self.srcLex['<sos>'] == hp.sos_idx
        assert self.srcLex['<eos>'] == self.srcLex['<eos>'] == hp.eos_idx
        assert self.srcLex['<unk>'] == self.srcLex['<unk>'] == hp.unk_idx
        return self.srcLex, self.tgtLex


def collate_batch(batch, srcLex, tgtLex):
    source, target = [], []
    for f, e in batch:
        source.append(torch.tensor([srcLex[f_tok] for f_tok in ['<sos>'] + f + ['<eos>']]))
        target.append(torch.tensor([tgtLex[e_tok] for e_tok in ['<sos>'] + e + ['<eos>']]))

    source = pad_sequence(source, padding_value=hp.pad_idx)
    target = pad_sequence(target, padding_value=hp.pad_idx)
    return source.to(hp.device), target.to(hp.device)


def loadTestData(srcFile, srcLex, device=0, linesToLoad=sys.maxsize):
    test_iter = Dataset(srcFile, srcFile, srcLex, srcLex, linesToLoad=linesToLoad)
    test_dl = DataLoader(list(test_iter), batch_size=1, shuffle=False, 
                         collate_fn=lambda batch:collate_batch(batch, srcLex, srcLex))
    return test_dl

if __name__ == '__main__':
    optparser = optparse.OptionParser()
    optparser.add_option(
        "-m", "--model", dest="model", default=os.path.join('data', 'seq2seq_E049.pt'), 
        help="model file")
    optparser.add_option(
        "-i", "--input", dest="input", default=os.path.join('data', 'input', 'dev.txt'),
        help="input file")
    optparser.add_option(
        "-n", "--num", dest="num", default=sys.maxsize, type='int',
        help="num of lines to load")
    (opts, _) = optparser.parse_args()

    model = Seq2Seq(build=False)
    model.load(opts.model)
    model.to(hp.device)
    model.eval()
    # loading test dataset

    test_dl = loadTestData(opts.input, model.params['srcLex'],
                           device=hp.device, linesToLoad=opts.num)
    results = translate(model, test_dl)
    print("\n".join(results))

