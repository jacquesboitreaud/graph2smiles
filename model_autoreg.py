# -*- coding: utf-8 -*-
"""
Created on Mon Apr 22 11:44:23 2019

@author: jacqu

Graph to sequence molecular VAE
RGCN encoder, GRU decoder to SELFIES 

No teacher forcing : autoregressive RNN decoder 


"""
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from queue import PriorityQueue
import json

from rdkit import Chem

import dgl
from dgl import mean_nodes
from dgl import function as fn
from dgl.nn.pytorch.glob import SumPooling
from dgl.nn.pytorch.conv import GATConv, RelGraphConv

from utils import *
from beam_search import BeamSearchNode
import pickle

class GRU_Decoder(nn.Module):
    
    def __init__(self, latent_dimension, gru_stack_size, gru_neurons_num, n_chars ):
        """
        Through Decoder
        """
        super(GRU_Decoder, self).__init__()
        self.gru_stack_size = gru_stack_size
        self.gru_neurons_num = gru_neurons_num
        self.n_chars = n_chars

        # Simple Decoder
        self.decode_RNN  = nn.GRU(
                input_size  = latent_dimension, 
                hidden_size = gru_neurons_num,
                num_layers  = gru_stack_size,
                batch_first = False)                
        
        self.decode_FC = nn.Sequential(
            nn.Linear(gru_neurons_num, self.n_chars),
        )
    

    def init_hidden(self, batch_size = 1):
        weight = next(self.parameters())
        return weight.new_zeros(self.gru_stack_size, batch_size, self.gru_neurons_num)
                 
                       
    def forward(self, z, hidden):
        """
        A forward pass throught the entire model.
        """
        # Decode
        l1, hidden = self.decode_RNN(z, hidden)    
        decoded = self.decode_FC(l1)  # fully connected layer

        return decoded, hidden

class RGCN(nn.Module):
    """ RGCN encoder with num_hidden_layers + 2 RGCN layers, and sum pooling. """
    def __init__(self, features_dim, h_dim, num_rels, num_layers, num_bases=-1):
        super(RGCN, self).__init__()
        
        self.features_dim, self.h_dim = features_dim, h_dim
        self.num_layers= num_layers
        
        self.num_rels = num_rels
        self.num_bases = num_bases
        # create rgcn layers
        self.build_model()
        self.pool = SumPooling()

    def build_model(self):
        self.layers = nn.ModuleList()
        # input to hidden
        i2h = RelGraphConv(self.features_dim, self.h_dim, self.num_rels, activation=nn.ReLU())
        self.layers.append(i2h)
        # hidden to hidden
        for _ in range(self.num_layers-2):
            h2h = RelGraphConv(self.h_dim, self.h_dim, self.num_rels, activation=nn.ReLU())
            self.layers.append(h2h)
        # hidden to output
        h2o = RelGraphConv(self.h_dim, self.h_dim, self.num_rels, activation=nn.ReLU())
        self.layers.append(h2o)
        
    def forward(self, g):
        sequence = []
        for i,layer in enumerate(self.layers):
            # Node update 
             g.ndata['h']=layer(g,g.ndata['h'],g.edata['one_hot'])
             # Jumping knowledge connexion 
             sequence.append(g.ndata['h'])
        # Concatenation :
        g.ndata['h'] = torch.cat(sequence, dim = 1) # Num_nodes * (h_dim*num_layers)
        out=self.pool(g,g.ndata['h'].view(len(g.nodes),-1,self.h_dim*self.num_layers) )
        return out
    
class Model(nn.Module):
    def __init__(self, features_dim, num_rels,
                 l_size, voc_size, max_len, 
                 N_properties, N_targets, binned_scores,
                 device,
                 index_to_char):
        super(Model, self).__init__()
        
        # params:
        
        # Encoding
        self.features_dim = features_dim
        self.gcn_hdim = 32
        self.gcn_layers = 3 # input, hidden , final.
        self.GRU_hdim = 512
        self.num_rels = num_rels
                
        # Bottleneck
        self.l_size = l_size
        
        # Decoding
        self.voc_size = voc_size 
        self.max_len = max_len
        self.index_to_char= index_to_char
        
        self.N_properties=N_properties
        self.N_targets = N_targets
        if(binned_scores):
            print('Use model from model.py for binned affinities multitasking. Not implemented here')
            raise NotImplementedError
        
        self.device = device
        
        # layers:
        self.encoder=RGCN(self.features_dim, self.gcn_hdim, self.num_rels, self.gcn_layers,
                          num_bases=-1).to(self.device)
        
        self.encoder_mean = nn.Linear(self.gcn_hdim*self.gcn_layers , self.l_size)
        self.encoder_logv = nn.Linear(self.gcn_hdim*self.gcn_layers , self.l_size)
        
        self.rnn_in= nn.Linear(self.l_size,self.voc_size)
        self.decoder = GRU_Decoder(latent_dimension= self.l_size, gru_stack_size=3, 
                                   gru_neurons_num=self.GRU_hdim, n_chars = self.voc_size )
        
        # MOLECULAR PROPERTY REGRESSOR
        self.MLP=nn.Sequential(
                nn.Linear(self.l_size,32),
                nn.ReLU(),
                nn.Linear(32,16),
                nn.ReLU(),
                nn.Linear(16,self.N_properties))
            
        # Affinities predictor (regression)
        self.aff_net = nn.Sequential(
                nn.Linear(self.l_size,32),
                nn.ReLU(),
                nn.Linear(32,16),
                nn.ReLU(),
                nn.Linear(16,min(1,self.N_targets)))
        
    def load(self, trained_path, aff_net=False):
        # Loads trained model weights, with or without the affinity predictor
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        if(aff_net):
            self.load_state_dict(torch.load(trained_path))
        else:
            self.load_no_aff_net(trained_path)
        self.to(device)
        print(f'Loaded weights from {trained_path} to {device}')
        
        return device
        
    # ======================== Model pass functions ==========================
    
    def forward(self, g, smiles):
        #print('edge data size ', g.edata['one_hot'].size())
        e_out = self.encoder(g)
        mu, logv = self.encoder_mean(e_out), self.encoder_logv(e_out)
        z= self.sample(mu, logv, mean_only=False).squeeze() # stochastic sampling 
        
        out = self.decode(z)
        
        properties = self.MLP(z)
        affinities = self.aff_net(z)
        
        return mu, logv,z, out, properties, affinities
        
    def sample(self, mean, logv, mean_only):
        """ Samples a vector according to the latent vector mean and variance """
        if not mean_only:
            sigma = torch.exp(.5 * logv)
            return mean + torch.randn_like(mean) * sigma
        else:
            return mean
        
    def encode(self, g, mean_only):
        """ Encodes to latent space, with or without stochastic sampling """
        e_out = self.encoder(g)
        mu, logv = self.encoder_mean(e_out), self.encoder_logv(e_out)
        z= self.sample(mu, logv, mean_only).squeeze() # train to true for stochastic sampling 
        return z
    
    def props(self,z):
        # Returns predicted properties
        return self.MLP(z)
    
    def affs(self,z):
        # returns predicted affinities 
        return self.aff_net(z)

        
    def decode(self, z ):
        """
            Unrolls decoder RNN to generate a batch of sequences, using teacher forcing
            Args:
                z: (batch_size * latent_shape) : a sampled vector in latent space
                x_true: (batch_size * sequence_length ) a batch of indices of sequences 
            Outputs:
                gen_seq : (batch_size * voc_size* seq_length) a batch of generated sequences (probas)
                
        """
        batch_size=z.shape[0]
        #ls= z.shape[1]
        #print('batch size is', batch_size, 'latent size is ', ls)
        z= z.reshape(1, batch_size, z.shape[1])
        
        hidden = self.decoder.init_hidden(batch_size = batch_size)
                                                                       
        # decoding from RNN N times, where N is the length of the largest molecule (all molecules are padded)
        decoded_one_hot = torch.zeros(batch_size, self.max_len, self.voc_size).to(self.device) 
        
        for seq_index in range(self.max_len):
            decoded_one_hot_line, hidden  = self.decoder(z , hidden)
            decoded_one_hot[:, seq_index, :] = decoded_one_hot_line[0]
        
        decoded_one_hot = decoded_one_hot.reshape(batch_size , self.voc_size, self.max_len) # for CE loss : N, C, d1...

        return decoded_one_hot
    
    def probas_to_smiles(self, gen_seq):
        # Takes tensor of shape (N, voc_size, seq_len), returns list of corresponding smiles
        N, voc_size, seq_len = gen_seq.shape
        v, indices = torch.max(gen_seq, dim=1)
        indices = indices.cpu().numpy()
        smiles = []
        for i in range(N):
            smiles.append(''.join([self.index_to_char[idx] for idx in indices[i]]).rstrip())
        return smiles
    
    def indices_to_smiles(self, indices):
        # Takes indices tensor of shape (N, seq_len), returns list of corresponding smiles
        N, seq_len = indices.shape
        try:
            indices = indices.cpu().numpy()
        except:
            pass
        smiles = []
        for i in range(N):
            smiles.append(''.join([self.index_to_char[idx] for idx in indices[i]]).rstrip())
        return smiles
    
    def beam_out_to_smiles(self,indices):
        """ Takes array of possibilities : (N, k_beam, sequences_length)  returned by decode_beam"""
        N, k_beam, length = indices.shape
        smiles = []
        for i in range(N):
            k,m = 0, None 
            while(k<2 and m==None):
                smi = ''.join([self.index_to_char[idx] for idx in indices[i,k]])
                smi = smi.rstrip()
                m=Chem.MolFromSmiles(smi)
                k+=1
            smiles.append(smi)
            print(smi)
        return smiles
    
    def decode_beam(self, z, k=3, cutoff_mols=None):
        """
        Input:
            z = torch.tensor type, (N_mols*l_size)  
            k : beam param
        Decodes a batch, molecule by molecule, using beam search of width k 
        Output: 
            a list of lists of k best sequences for each molecule.
        """
        N = z.shape[0]
        if(cutoff_mols!=None):
            N=cutoff_mols
            print(f'Decoding will stop after {N} mols')
        sequences = []
        for n in range(N):
            print("decoding molecule n° ",n)
            # Initialize rnn states and input
            z_1mol=z[n].view(1,self.l_size) # Reshape as a batch of size 1
            start_token = self.rnn_in(z_1mol).view(1,self.voc_size,1).to(self.device)
            rnn_in = start_token
            h = self.decoder.init_h(z_1mol)
            topk = [BeamSearchNode(h,rnn_in, 0, [] )]
            
            for step in range(self.max_len):
                next_nodes=PriorityQueue()
                for candidate in topk: # for each candidate sequence (among k)
                    score = candidate.score
                    seq=candidate.sequence
                    # pass into decoder
                    out, new_h = self.decoder(candidate.rnn_in, candidate.h) 
                    probas = F.softmax(out, dim=1) # Shape N, voc_size
                    for c in range(self.voc_size):
                        new_seq=seq+[c]
                        rnn_in=torch.zeros((1,36))
                        rnn_in[0,c]=1
                        s= score-probas[0,c]
                        next_nodes.put(( s.item(), BeamSearchNode(new_h, rnn_in.to(self.device),s.item(), new_seq)) )
                topk=[]
                for k_ in range(k):
                    # get top k for next timestep !
                    score, node=next_nodes.get()
                    topk.append(node)
                    #print("top sequence for next step :", node.sequence)
                    
            sequences.append([n.sequence for n in topk]) # list of lists 
        return np.array(sequences)
    
    # ========================== Sampling functions ======================================
    
    def sample_around_mol(self, g, dist, beam_search=False, attempts = 1, props=False, aff=False):
        """ Samples around embedding of molecular graph g, within a l2 distance of d """
        e_out = self.encoder(g)
        mu, var = self.encoder_mean(e_out), self.encoder_logv(e_out)
        sigma = torch.exp(.5 * var)
        
        tensors_list = []
        for i in range(attempts):
            noise = torch.randn_like(mu) * sigma
            noise = (dist/torch.norm(noise,p=2,dim=1))*noise # rescale noise norm to be equal to dist 
            noise = noise.to(self.device)
            sp=mu + noise 
            tensors_list.append(sp)
        
        if(attempts>1):
            samples=torch.stack(tensors_list, dim=0)
            samples = torch.squeeze(samples)
        else:
            samples = sp
            
        if(beam_search):
            dec = self.decode_beam(samples)
        else:
            dec = self.decode(samples)
            
        # props ad affinity if requested 
        p,a = 0,0
        if(props):
            p = self.props(samples)
        if(aff):
            a = self.aff(samples)
        
        return dec, p, a
    
    def sample_around_z(self, z, dist, beam_search=False, attempts = 1, props=False, aff=False):
        """ Samples around embedding of molecular graph g, within a l2 distance of d """

        sigma = torch.exp(.5 * torch.randn_like(z)).to(self.device)
        z=z.to(self.device)
        tensors_list = []
        for i in range(attempts):
            noise = torch.randn_like(z) * sigma
            noise = (dist/torch.norm(noise,p=2,dim=1))*noise # rescale noise norm to be equal to dist 
            noise = noise.to(self.device)
            sp=z + noise 
            tensors_list.append(sp)
        
        if(attempts>1):
            samples=torch.stack(tensors_list, dim=0)
            samples = torch.squeeze(samples)
        else:
            samples = sp
        """
        if(beam_search):
            dec = self.decode_beam(samples)
        else:
            dec = self.decode(samples)
            
        # props ad affinity if requested 
        p,a = 0,0
        if(props):
            p = self.props(samples)
        if(aff):
            a = self.aff(samples)
        
        return dec, p, a
        """
        return samples
    
    def sample_z_prior(self, n_mols):
        """Sampling z ~ p(z) = N(0, I)
        :param n_batch: number of batches
        :return: (n_batch, d_z) of floats, sample of latent z
        """
        latent_points = []
        for i in range(n_mols):
            latent_points.append(torch.normal(torch.zeros(self.l_size),torch.ones(self.l_size)).view(1, self.l_size) )
            
        latent = torch.cat(latent_points, dim = 0 )
        
        return latent.to(self.device)
    
    # ========================= Packaged functions to use trained model ========================
    
    def embed(self, loader, df):
    # Gets latent embeddings of molecules in df. 
    # Inputs : 
    # 0. loader object to convert smiles into batches of inputs 
    # 1. dataframe with 'can' column containing smiles to embed 
    # Outputs :
    # 0. np array of embeddings, (N_molecules , latent_size)
    
        loader.dataset.pass_dataset(df)
        _, _, test_loader = loader.get_data()
        batch_size=loader.batch_size
        
        # Latent embeddings
        z_all = torch.zeros(loader.dataset.n,self.l_size)
        
        with torch.no_grad():
            for batch_idx, (graph, smiles, p_target, a_target) in enumerate(test_loader):
                
                graph=send_graph_to_device(graph,self.device)
            
                z = self.encode(graph, mean_only=True) #z_shape = N * l_size
                z=z.cpu()
                z_all[batch_idx*batch_size:(batch_idx+1)*batch_size]=z
                
        z_all = z_all.numpy()
        return z_all
    
    def load_no_aff_net(self, state_dict):
        # Workaround to be able to load a model with not same size of affinity predictor... 
        pretrained_dict = torch.load(state_dict)
        model_dict = self.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict and v.size() == model_dict[k].size()}
        # 2. overwrite entries in the existing state dict
        model_dict.update(pretrained_dict) 
        # 3. load the new state dict
        self.load_state_dict(model_dict)