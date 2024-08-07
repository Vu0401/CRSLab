# @Time   : 2020/11/22
# @Author : Kun Zhou
# @Email  : francis_kun_zhou@163.com

# UPDATE:
# @Time   : 2020/11/24, 2020/12/29, 2021/1/4
# @Author : Kun Zhou, Xiaolei Wang, Yuanhang Zhou
# @Email  : francis_kun_zhou@163.com, wxl1999@foxmail.com, sdzyh002@gmail.com

r"""
KGSF
====
References:
    Zhou, Kun, et al. `"Improving Conversational Recommender Systems via Knowledge Graph based Semantic Fusion."`_ in KDD 2020.

.. _`"Improving Conversational Recommender Systems via Knowledge Graph based Semantic Fusion."`:
   https://dl.acm.org/doi/abs/10.1145/3394486.3403143

"""

import os

import numpy as np
import torch
import torch.nn.functional as F
from loguru import logger
from torch import nn
from torch_geometric.nn import GCNConv, RGCNConv

from crslab.config import MODEL_PATH
from crslab.model.base import BaseModel
from crslab.model.utils.functions import edge_to_pyg_format
from crslab.model.utils.modules.attention import SelfAttentionSeq
from crslab.model.utils.modules.transformer import TransformerEncoder
from .modules import GateLayer, TransformerDecoderKG
from .resources import resources


class KGSFModel(BaseModel):
    """

    Attributes:
        vocab_size: A integer indicating the vocabulary size.
        pad_token_idx: A integer indicating the id of padding token.
        start_token_idx: A integer indicating the id of start token.
        end_token_idx: A integer indicating the id of end token.
        token_emb_dim: A integer indicating the dimension of token embedding layer.
        pretrain_embedding: A string indicating the path of pretrained embedding.
        n_word: A integer indicating the number of words.
        n_entity: A integer indicating the number of entities.
        pad_word_idx: A integer indicating the id of word padding.
        pad_entity_idx: A integer indicating the id of entity padding.
        num_bases: A integer indicating the number of bases.
        kg_emb_dim: A integer indicating the dimension of kg embedding.
        n_heads: A integer indicating the number of heads.
        n_layers: A integer indicating the number of layer.
        ffn_size: A integer indicating the size of ffn hidden.
        dropout: A float indicating the dropout rate.
        attention_dropout: A integer indicating the dropout rate of attention layer.
        relu_dropout: A integer indicating the dropout rate of relu layer.
        learn_positional_embeddings: A boolean indicating if we learn the positional embedding.
        embeddings_scale: A boolean indicating if we use the embeddings scale.
        reduction: A boolean indicating if we use the reduction.
        n_positions: A integer indicating the number of position.
        response_truncate = A integer indicating the longest length for response generation.
        pretrained_embedding: A string indicating the path of pretrained embedding.

    """

    def __init__(self, opt, device, vocab, side_data):
        """

        Args:
            opt (dict): A dictionary record the hyper parameters.
            device (torch.device): A variable indicating which device to place the data and model.
            vocab (dict): A dictionary record the vocabulary information.
            side_data (dict): A dictionary record the side data.

        """
        self.device = device
        self.gpu = opt.get("gpu", [-1])
        # vocab
        self.vocab_size = vocab['vocab_size']
        self.pad_token_idx = vocab['pad']
        self.start_token_idx = vocab['start']
        self.end_token_idx = vocab['end']
        self.token_emb_dim = opt['token_emb_dim']
        self.pretrained_embedding = side_data.get('embedding', None)
        # kg
        self.n_word = vocab['n_word']
        self.n_entity = vocab['n_entity']
        self.pad_word_idx = vocab['pad_word']
        self.pad_entity_idx = vocab['pad_entity']
        entity_kg = side_data['entity_kg']
        self.n_relation = entity_kg['n_relation']
        entity_edges = entity_kg['edge']
        self.entity_edge_idx, self.entity_edge_type = edge_to_pyg_format(entity_edges, 'RGCN')
        self.entity_edge_idx = self.entity_edge_idx.to(device)
        self.entity_edge_type = self.entity_edge_type.to(device)
        word_edges = side_data['word_kg']['edge']

        self.word_edges = edge_to_pyg_format(word_edges, 'GCN').to(device)

        self.num_bases = opt['num_bases']
        self.kg_emb_dim = opt['kg_emb_dim']
        # transformer
        self.n_heads = opt['n_heads']
        self.n_layers = opt['n_layers']
        self.ffn_size = opt['ffn_size']
        self.dropout = opt['dropout']
        self.attention_dropout = opt['attention_dropout']
        self.relu_dropout = opt['relu_dropout']
        self.learn_positional_embeddings = opt['learn_positional_embeddings']
        self.embeddings_scale = opt['embeddings_scale']
        self.reduction = opt['reduction']
        self.n_positions = opt['n_positions']
        self.response_truncate = opt.get('response_truncate', 20)
        # encoder
        self.transformer_config = {
            'n_heads': opt.get('n_heads', 2),
            'n_layers': opt.get('n_layers', 2),
            'embedding_size': self.token_emb_dim,
            'ffn_size': opt.get('ffn_size', 300),
            'vocabulary_size': self.vocab_size,
            'embedding': None,
            'dropout': opt.get('dropout', 0.1),
            'attention_dropout': opt.get('attention_dropout', 0.0),
            'relu_dropout': opt.get('relu_dropout', 0.1),
            'padding_idx': self.pad_token_idx,
            'learn_positional_embeddings': opt.get('learn_positional_embeddings', False),
            'embeddings_scale': opt.get('embedding_scale', True),
            'reduction': opt.get('reduction', False),
            'n_positions': opt.get('n_positions', 1024)
        }
        # copy mask
        dataset = opt['dataset']
        dpath = os.path.join(MODEL_PATH, "kgsf", dataset)
        resource = resources[dataset]
        super(KGSFModel, self).__init__(opt, device, dpath, resource)

    def build_model(self):
        self._init_embeddings()
        self._build_kg_layer()
        self._build_infomax_layer()
        self._build_recommendation_layer()
        self._build_conversation_layer()

    def _init_embeddings(self):
        if self.pretrained_embedding is not None:
            self.token_embedding = nn.Embedding.from_pretrained(
                torch.as_tensor(self.pretrained_embedding, dtype=torch.float), freeze=False,
                padding_idx=self.pad_token_idx)
        else:
            self.token_embedding = nn.Embedding(self.vocab_size, self.token_emb_dim, self.pad_token_idx)
            nn.init.normal_(self.token_embedding.weight, mean=0, std=self.kg_emb_dim ** -0.5)
            nn.init.constant_(self.token_embedding.weight[self.pad_token_idx], 0)

        self.word_kg_embedding = nn.Embedding(self.n_word, self.kg_emb_dim, self.pad_word_idx)
        nn.init.normal_(self.word_kg_embedding.weight, mean=0, std=self.kg_emb_dim ** -0.5)
        nn.init.constant_(self.word_kg_embedding.weight[self.pad_word_idx], 0)

        logger.debug('[Finish init embeddings]')

    def _build_kg_layer(self):
        # db encoder
        self.entity_encoder = RGCNConv(self.n_entity, self.kg_emb_dim, self.n_relation, self.num_bases)
        self.entity_self_attn = SelfAttentionSeq(self.kg_emb_dim, self.kg_emb_dim)

        # concept encoder
        self.word_encoder = GCNConv(self.kg_emb_dim, self.kg_emb_dim)
        self.word_self_attn = SelfAttentionSeq(self.kg_emb_dim, self.kg_emb_dim)

        # gate mechanism
        self.gate_layer = GateLayer(self.kg_emb_dim)

        logger.debug('[Finish build kg layer]')

    def _build_infomax_layer(self):
        self.infomax_norm = nn.Linear(self.kg_emb_dim, self.kg_emb_dim)
        self.infomax_bias = nn.Linear(self.kg_emb_dim, self.n_entity)
        self.infomax_loss = nn.MSELoss(reduction='sum')

        logger.debug('[Finish build infomax layer]')

    def _build_recommendation_layer(self):
        self.rec_bias = nn.Linear(self.kg_emb_dim, self.n_entity)
        self.rec_loss = nn.CrossEntropyLoss()

        logger.debug('[Finish build rec layer]')

    def _build_conversation_layer(self):
        self.register_buffer('START', torch.tensor([self.start_token_idx], dtype=torch.long))
        self.conv_encoder = TransformerEncoder(
            self.transformer_config
        )

        self.conv_entity_norm = nn.Linear(self.kg_emb_dim, self.ffn_size)
        self.conv_entity_attn_norm = nn.Linear(self.kg_emb_dim, self.ffn_size)
        self.conv_word_norm = nn.Linear(self.kg_emb_dim, self.ffn_size)
        self.conv_word_attn_norm = nn.Linear(self.kg_emb_dim, self.ffn_size)

        self.copy_norm = nn.Linear(self.ffn_size * 3, self.token_emb_dim)
        self.copy_output = nn.Linear(self.token_emb_dim, self.vocab_size)
        self.copy_mask = torch.as_tensor(np.load(os.path.join(self.dpath, "copy_mask.npy")).astype(bool),
                                         ).to(self.device)

        self.conv_decoder = TransformerDecoderKG(
            self.n_heads, self.n_layers, self.token_emb_dim, self.ffn_size, self.vocab_size,
            embedding=self.token_embedding,
            dropout=self.dropout,
            attention_dropout=self.attention_dropout,
            relu_dropout=self.relu_dropout,
            embeddings_scale=self.embeddings_scale,
            learn_positional_embeddings=self.learn_positional_embeddings,
            padding_idx=self.pad_token_idx,
            n_positions=self.n_positions
        )
        self.conv_loss = nn.CrossEntropyLoss(ignore_index=self.pad_token_idx)

        logger.debug('[Finish build conv layer]')

    def pretrain_infomax(self, batch):
        """
        words: (batch_size, word_length)
        entity_labels: (batch_size, n_entity)
        """
        words, entity_labels = batch

        loss_mask = torch.sum(entity_labels)
        if loss_mask.item() == 0:
            return None

        entity_graph_representations = self.entity_encoder(None, self.entity_edge_idx, self.entity_edge_type)
        word_graph_representations = self.word_encoder(self.word_kg_embedding.weight, self.word_edges)

        word_representations = word_graph_representations[words]
        word_padding_mask = words.eq(self.pad_word_idx)  # (bs, seq_len)

        word_attn_rep = self.word_self_attn(word_representations, word_padding_mask)
        word_info_rep = self.infomax_norm(word_attn_rep)  # (bs, dim)
        info_predict = F.linear(word_info_rep, entity_graph_representations, self.infomax_bias.bias)  # (bs, #entity)
        loss = self.infomax_loss(info_predict, entity_labels) / loss_mask
        return loss

    def recommend(self, batch, mode):
        """
        context_entities: (batch_size, entity_length)
        context_words: (batch_size, word_length)
        movie: (batch_size)
        """
        context_entities, context_words, entities, movie = batch

        entity_graph_representations = self.entity_encoder(None, self.entity_edge_idx, self.entity_edge_type)
        word_graph_representations = self.word_encoder(self.word_kg_embedding.weight, self.word_edges)

        entity_padding_mask = context_entities.eq(self.pad_entity_idx)  # (bs, entity_len)
        word_padding_mask = context_words.eq(self.pad_word_idx)  # (bs, word_len)

        entity_representations = entity_graph_representations[context_entities]
        word_representations = word_graph_representations[context_words]

        entity_attn_rep = self.entity_self_attn(entity_representations, entity_padding_mask)
        word_attn_rep = self.word_self_attn(word_representations, word_padding_mask)

        user_rep = self.gate_layer(entity_attn_rep, word_attn_rep)
        rec_scores = F.linear(user_rep, entity_graph_representations, self.rec_bias.bias)  # (bs, #entity)

        rec_loss = self.rec_loss(rec_scores, movie)

        info_loss_mask = torch.sum(entities)
        if info_loss_mask.item() == 0:
            info_loss = None
        else:
            word_info_rep = self.infomax_norm(word_attn_rep)  # (bs, dim)
            info_predict = F.linear(word_info_rep, entity_graph_representations,
                                    self.infomax_bias.bias)  # (bs, #entity)
            info_loss = self.infomax_loss(info_predict, entities) / info_loss_mask

        return rec_loss, info_loss, rec_scores

    def freeze_parameters(self):
        freeze_models = [self.word_kg_embedding, self.entity_encoder, self.entity_self_attn, self.word_encoder,
                         self.word_self_attn, self.gate_layer, self.infomax_bias, self.infomax_norm, self.rec_bias]
        for model in freeze_models:
            for p in model.parameters():
                p.requires_grad = False

    def _starts(self, batch_size):
        """Return bsz start tokens."""
        return self.START.detach().expand(batch_size, 1)

    def _decode_forced_with_kg(self, token_encoding, entity_reps, entity_emb_attn, entity_mask,
                               word_reps, word_emb_attn, word_mask, response):
        batch_size, seq_len = response.shape
        start = self._starts(batch_size)
        inputs = torch.cat((start, response[:, :-1]), dim=-1).long()

        dialog_latent, _ = self.conv_decoder(inputs, token_encoding, word_reps, word_mask,
                                             entity_reps, entity_mask)  # (bs, seq_len, dim)
        entity_latent = entity_emb_attn.unsqueeze(1).expand(-1, seq_len, -1)
        word_latent = word_emb_attn.unsqueeze(1).expand(-1, seq_len, -1)
        copy_latent = self.copy_norm(
            torch.cat((entity_latent, word_latent, dialog_latent), dim=-1))  # (bs, seq_len, dim)

        copy_logits = self.copy_output(copy_latent) * self.copy_mask.unsqueeze(0).unsqueeze(
            0)  # (bs, seq_len, vocab_size)
        gen_logits = F.linear(dialog_latent, self.token_embedding.weight)  # (bs, seq_len, vocab_size)
        sum_logits = copy_logits + gen_logits
        preds = sum_logits.argmax(dim=-1)
        return sum_logits, preds

    def _decode_greedy_with_kg(self, token_encoding, entity_reps, entity_emb_attn, entity_mask,
                               word_reps, word_emb_attn, word_mask):
        batch_size = token_encoding[0].shape[0]
        inputs = self._starts(batch_size).long()
        incr_state = None
        logits = []
        for _ in range(self.response_truncate):
            dialog_latent, incr_state = self.conv_decoder(inputs, token_encoding, word_reps, word_mask,
                                                          entity_reps, entity_mask, incr_state)
            dialog_latent = dialog_latent[:, -1:, :]  # (bs, 1, dim)
            db_latent = entity_emb_attn.unsqueeze(1)
            concept_latent = word_emb_attn.unsqueeze(1)
            copy_latent = self.copy_norm(torch.cat((db_latent, concept_latent, dialog_latent), dim=-1))

            copy_logits = self.copy_output(copy_latent) * self.copy_mask.unsqueeze(0).unsqueeze(0)
            gen_logits = F.linear(dialog_latent, self.token_embedding.weight)
            sum_logits = copy_logits + gen_logits
            preds = sum_logits.argmax(dim=-1).long()
            logits.append(sum_logits)
            inputs = torch.cat((inputs, preds), dim=1)

            finished = ((inputs == self.end_token_idx).sum(dim=-1) > 0).sum().item() == batch_size
            if finished:
                break
        logits = torch.cat(logits, dim=1)
        return logits, inputs

    def decode_beam_search_with_kg(self, token_encoding, entity_reps, entity_emb_attn, entity_mask, word_reps, word_emb_attn, word_mask, beam=4):
        batch_size = token_encoding[0].shape[0]
        sequences, inputs, incr_state = self._initialize_beam_search(batch_size, beam)

        for i in range(self.response_truncate):
            if i == 1:
                token_encoding, entity_reps, entity_emb_attn, entity_mask, word_reps, word_emb_attn, word_mask = \
                    self._repeat_inputs(token_encoding, entity_reps, entity_emb_attn, entity_mask, word_reps, word_emb_attn, word_mask, beam)

            if i != 0:
                inputs = self._get_inputs_from_sequences(sequences, beam, batch_size)
            #logits, copy_logits, gen_logits
            logits, _, _ = self._compute_logits(inputs, token_encoding, word_reps, word_mask, entity_reps, entity_mask, incr_state, entity_emb_attn, word_emb_attn)
            
            probs, preds = self._get_top_k_probabilities(logits, beam)
            
            sequences = self._update_sequences(sequences, inputs, logits, probs, preds, beam, batch_size)

            if self._is_generation_finished(inputs):
                break

        return self._get_final_outputs(sequences)

    def _initialize_beam_search(self, batch_size, beam):
        inputs = self._starts(batch_size).long().reshape(1, batch_size, -1)
        sequences = [[[list(), list(), 1.0]]] * batch_size
        incr_state = None
        return sequences, inputs, incr_state

    def _repeat_inputs(self, token_encoding, entity_reps, entity_emb_attn, entity_mask, word_reps, word_emb_attn, word_mask, beam):
        return (
            (token_encoding[0].repeat(beam, 1, 1), token_encoding[1].repeat(beam, 1, 1)),
            entity_reps.repeat(beam, 1, 1),
            entity_emb_attn.repeat(beam, 1),
            entity_mask.repeat(beam, 1),
            word_reps.repeat(beam, 1, 1),
            word_emb_attn.repeat(beam, 1),
            word_mask.repeat(beam, 1)
        )

    def _get_inputs_from_sequences(self, sequences, beam, batch_size):
        return torch.stack([seq[0][0] for seq in sequences]).reshape(beam, batch_size, -1)

    def _compute_logits(self, inputs, token_encoding, word_reps, word_mask, entity_reps, entity_mask, incr_state, entity_emb_attn, word_emb_attn):
        with torch.no_grad():
            dialog_latent, incr_state = self.conv_decoder(
                inputs.reshape(-1, inputs.size(-1)),
                token_encoding, word_reps, word_mask, entity_reps, entity_mask, incr_state
            )
        dialog_latent = dialog_latent[:, -1:, :]
        copy_latent = self.copy_norm(torch.cat((entity_emb_attn.unsqueeze(1), word_emb_attn.unsqueeze(1), dialog_latent), dim=-1))
        copy_logits = self.copy_output(copy_latent) * self.copy_mask.unsqueeze(0).unsqueeze(0)
        gen_logits = F.linear(dialog_latent, self.token_embedding.weight)
        logits = copy_logits + gen_logits
        return logits, copy_logits, gen_logits

    def _get_top_k_probabilities(self, logits, beam):
        return torch.nn.functional.softmax(logits).topk(beam, dim=-1)

    def _update_sequences(self, sequences, inputs, logits, probs, preds, beam, batch_size):
        new_sequences = []
        for j in range(batch_size):
            candidates = [
                (
                    torch.cat((inputs[n][j].reshape(-1), preds[n][j][0][k].reshape(-1))),
                    torch.cat((sequences[j][n][1], logits[n][j][0].unsqueeze(0))) if sequences[j][n][1] else logits[n][j][0].unsqueeze(0),
                    sequences[j][n][2] * probs[n][j][0][k]
                )
                for n in range(len(sequences[j]))
                for k in range(beam)
            ]
            new_sequences.append(sorted(candidates, key=lambda x: x[2], reverse=True)[:beam])
        return new_sequences

    def _is_generation_finished(self, inputs):
        return ((inputs == self.end_token_idx).sum(dim=1) > 0).sum().item() == inputs.size(1)

    def _get_final_outputs(self, sequences):
        logits = torch.stack([seq[0][1] for seq in sequences])
        inputs = torch.stack([seq[0][0] for seq in sequences])
        return logits, inputs

    def converse(self, batch, mode):
        context_tokens, context_entities, context_words, response = batch

        entity_graph_representations = self.entity_encoder(None, self.entity_edge_idx, self.entity_edge_type)
        word_graph_representations = self.word_encoder(self.word_kg_embedding.weight, self.word_edges)

        entity_padding_mask = context_entities.eq(self.pad_entity_idx)  # (bs, entity_len)
        word_padding_mask = context_words.eq(self.pad_word_idx)  # (bs, seq_len)

        entity_representations = entity_graph_representations[context_entities]
        word_representations = word_graph_representations[context_words]

        entity_attn_rep = self.entity_self_attn(entity_representations, entity_padding_mask)
        word_attn_rep = self.word_self_attn(word_representations, word_padding_mask)

        # encoder-decoder
        tokens_encoding = self.conv_encoder(context_tokens)
        conv_entity_emb = self.conv_entity_attn_norm(entity_attn_rep)
        conv_word_emb = self.conv_word_attn_norm(word_attn_rep)
        conv_entity_reps = self.conv_entity_norm(entity_representations)
        conv_word_reps = self.conv_word_norm(word_representations)
        if mode != 'test':
            logits, preds = self._decode_forced_with_kg(tokens_encoding, conv_entity_reps, conv_entity_emb,
                                                        entity_padding_mask,
                                                        conv_word_reps, conv_word_emb, word_padding_mask,
                                                        response)

            logits = logits.view(-1, logits.shape[-1])
            response = response.view(-1)
            loss = self.conv_loss(logits, response)
            return loss, preds
        else:
            logits, preds = self._decode_greedy_with_kg(tokens_encoding, conv_entity_reps, conv_entity_emb,
                                                        entity_padding_mask,
                                                        conv_word_reps, conv_word_emb, word_padding_mask)
            return preds

    def forward(self, batch, stage, mode):
        if len(self.gpu) >= 2:
            #  forward function operates on different gpus, the weight of graph network need to be copied to other gpu
            self.entity_edge_idx = self.entity_edge_idx.cuda(torch.cuda.current_device())
            self.entity_edge_type = self.entity_edge_type.cuda(torch.cuda.current_device())
            self.word_edges = self.word_edges.cuda(torch.cuda.current_device())
            self.copy_mask = torch.as_tensor(np.load(os.path.join(self.dpath, "copy_mask.npy")).astype(bool),
                                             ).cuda(torch.cuda.current_device())
        if stage == "pretrain":
            loss = self.pretrain_infomax(batch)
        elif stage == "rec":
            loss = self.recommend(batch, mode)
        elif stage == "conv":
            loss = self.converse(batch, mode)
        return loss
