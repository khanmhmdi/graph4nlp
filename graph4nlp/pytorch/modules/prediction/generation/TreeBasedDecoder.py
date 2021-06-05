import torch
import torch.nn as nn
import torch.nn.functional as F

import random
import numpy as np

from .base import RNNTreeDecoderBase
from ...utils.tree_utils import Tree, to_cuda
from .attention import Attention

from graph4nlp.pytorch.data.data import GraphData, from_batch
from graph4nlp.pytorch.modules.utils.tree_utils import to_cuda
from graph4nlp.pytorch.modules.prediction.generation.decoder_strategy import DecoderStrategy


class StdTreeDecoder(RNNTreeDecoderBase):
    r"""StdTreeDecoder: This is a tree decoder implementation, which is used for tree object decoding.

    Attributes
    ----------
    attn_type : str,
        Describe which attention mechanism is used, can be ``uniform``, ``separate_on_encoder_type``, ``separate_on_node_type``.

    embeddings : torch.nn.Module,
        Embedding layer, input is tensor of word index, output is word embedding tensor.

    enc_hidden_size : int, 
        Size of encoder hidden state.

    dec_emb_size : int,
        Size of decoder word embedding layer output size.

    dec_hidden_size : int,
        Size of decoder hidden state. (namely the ``lstm`` or ``gru`` hidden size when rnn unit has been specified)

    output_size : int,
        Size of output vocabulary size.

    device : object,
        Device where parameters and data are store, a torch.device object.

    teacher_force_ratio : float,
        The ratio of possibility to use teacher force training.

    use_sibling : boolean,
        Whether feed sibling state in each decoding step.

    use_copy : boolean,
        Whether use copy mechanism in decoding.

    fuse_strategy: str, option=[None, "average", "concatenate"], default=None
        The strategy to fuse attention results generated by separate attention.
        "None": If we do ``uniform`` attention, we will set it to None.
        "``average``": We will take an average on all results.
        "``concatenate``": We will concatenate all results to one.

    num_layers : int, optional,
        Layer number of decoder rnn unit.

    dropout_for_decoder: float,
        Dropout ratio for decoder(include both the dropout for word embedding and the dropout for attention layer)
    
    tgt_vocab : object,
        The vocab object used in decoder, including all the word<->id pairs appeared in the output sentences.
    
    graph_pooling_strategy : str,
        The graph pooling strategy used to generate the graph embedding with node embeddings

    rnn_type: str, optional,
        The rnn unit is used, option=["lstm", "gru"], default="lstm".

    max_dec_seq_length : int, optional,
        In decoding, the decoding steps upper limit.

    max_dec_tree_depth : int, optional,
        In decoding, the tree depth lower limit.
    """

    def __init__(self, attn_type, embeddings, enc_hidden_size, dec_emb_size,
                #  dec_hidden_size, output_size, device, criterion, teacher_force_ratio,
                 dec_hidden_size, output_size, criterion, teacher_force_ratio,
                 use_sibling=True, use_attention=True, use_copy=False,
                 fuse_strategy="average", num_layers=1,
                 dropout_for_decoder=0.1, rnn_type="lstm", max_dec_seq_length=512,
                 max_dec_tree_depth=256, tgt_vocab=None, graph_pooling_strategy="max"):

        super(StdTreeDecoder, self).__init__(use_attention=True,
                                             use_copy=use_copy,
                                             use_coverage=False,
                                             attention_type="uniform",
                                             fuse_strategy="average")
        self.num_layers = num_layers
        # self.device = device
        self.criterion = criterion
        self.rnn_size = dec_hidden_size
        self.enc_hidden_size = enc_hidden_size
        self.hidden_size = dec_hidden_size
        self.max_dec_seq_length = max_dec_seq_length
        self.max_dec_tree_depth = max_dec_tree_depth
        self.tgt_vocab = tgt_vocab
        self.teacher_force_ratio = teacher_force_ratio
        self.use_sibling = use_sibling
        self.dec_emb_size = dec_emb_size
        self.dropout_input = dropout_for_decoder
        self.embeddings = embeddings
        self.graph_pooling_strategy = graph_pooling_strategy

        self.attn_state = {}
        self.use_copy = use_copy
        self.attention = Attention(query_size=dec_hidden_size,
                                   memory_size=enc_hidden_size*2 if (enc_hidden_size*2 == dec_hidden_size) else enc_hidden_size,
                                   hidden_size=dec_hidden_size,
                                   has_bias=True, dropout=dropout_for_decoder,
                                   attention_funtion="dot")
        self.separate_attn = (attn_type != "uniform")

        if self.separate_attn:
            self.linear_att = nn.Linear(3*dec_hidden_size, dec_hidden_size)
        else:
            self.linear_att = nn.Linear(2*dec_hidden_size, dec_hidden_size)
        self.linear_out = nn.Linear(dec_hidden_size, output_size)
        self.dropout_attn = nn.Dropout(dropout_for_decoder)
        self.logsoftmax = nn.LogSoftmax(dim=1)

        if self.use_copy:
            ptr_size = self.embeddings.embedding_dim
            ptr_size += 4*self.rnn_size
            self.ptr = nn.Linear(ptr_size, 1)

        self.rnn = self._build_rnn(rnn_type=rnn_type, input_size=output_size, emb_size=dec_emb_size,
                                   hidden_size=dec_hidden_size, dropout_input=dropout_for_decoder, use_sibling=use_sibling)

    def _run_forward_pass(self, 
                          graph_node_embedding, 
                          graph_node_mask, 
                          rnn_node_embedding, 
                          graph_level_embedding,
                          graph_edge_embedding=None, 
                          graph_edge_mask=None, 
                          tgt_tree_batch=None, 
                          enc_batch=None, 
                          oov_dict=None):
        r"""
            The private calculation method for decoder.

        Parameters
        ----------
        enc_batch : torch.Tensor,
            The input batch : (Batch_size * Source sentence word index tensor).

        tgt_tree_batch:
            The target tree to generate : consists of (Batch_size * Tree object), each node in a Tree object is either a word index or a children Tree object.

        graph_node_embedding: torch.Tensor,
            The graph node embedding matrix of shape :math:`(B, N, D_{in})`

        graph_node_mask: torch.Tensor,
            The graph node type mask matrix of shape :math`(B, N)`

        rnn_node_embedding: torch.Tensor,
            The rnn encoded embedding matrix of shape :math`(B, N, D_{in})`

        graph_level_embedding: torch.Tensor,
            graph level embedding of shape :math`(B, D_{in})`

        graph_edge_embedding: torch.Tensor,
            graph edge embedding of shape :math`(B, N, D_{in})`

        graph_edge_mask: torch.Tensor,
            graph edge type embedding

        oov_dict: dict,
            vocab dict used in copy mechanism to incorporate some new words which have never appeared in vocab for input sentences in training set.
        """
        tgt_batch_size = len(tgt_tree_batch)

        enc_outputs = graph_node_embedding

        if graph_level_embedding == None:
            if self.graph_pooling_strategy == "max":
                graph_level_embedding = torch.max(graph_node_embedding, 1)[0]
            elif self.graph_pooling_strategy == "min":
                graph_level_embedding = torch.min(graph_node_embedding, 1)[0]
            elif self.graph_pooling_strategy == "mean":
                graph_level_embedding = torch.mean(graph_node_embedding, 1)
            else:
                raise NotImplementedError()

            graph_cell_state = graph_level_embedding
            graph_hidden_state = graph_level_embedding
        else:
            graph_cell_state, graph_hidden_state = graph_level_embedding

        rnn_node_embedding = torch.zeros_like(
            graph_node_embedding, requires_grad=False)
        rnn_node_embedding = to_cuda(rnn_node_embedding, self.device)

        cur_index = 1
        loss = 0

        dec_batch, queue_tree, max_index = get_dec_batch(
            tgt_tree_batch, tgt_batch_size, self.device, self.tgt_vocab)

        dec_state = {}
        for i in range(self.max_dec_tree_depth + 1):
            dec_state[i] = {}
            for j in range(self.max_dec_seq_length + 1):
                dec_state[i][j] = {}

        while (cur_index <= max_index):
            if cur_index > self.max_dec_tree_depth:
                break
            for j in range(1, 3):
                dec_state[cur_index][0][j] = torch.zeros(
                    (tgt_batch_size, self.rnn_size), dtype=torch.float, requires_grad=False, device=self.device)
                dec_state[cur_index][0][j] = to_cuda(
                    dec_state[cur_index][0][j], self.device)

            sibling_state = torch.zeros(
                (tgt_batch_size, self.rnn_size), dtype=torch.float, requires_grad=False)
            sibling_state = to_cuda(sibling_state, self.device)

            # with torch.no_grad():
            if cur_index == 1:
                for i in range(tgt_batch_size):
                    dec_state[1][0][1][i, :] = graph_cell_state[i]
                    dec_state[1][0][2][i, :] = graph_hidden_state[i]

            else:
                for i in range(1, tgt_batch_size+1):
                    if (cur_index <= len(queue_tree[i])):
                        par_index = queue_tree[i][cur_index - 1]["parent"]
                        child_index = queue_tree[i][cur_index -
                                                    1]["child_index"]

                        dec_state[cur_index][0][1][i-1,
                                                   :] = dec_state[par_index][child_index][1][i-1, :]
                        dec_state[cur_index][0][2][i-1,
                                                   :] = dec_state[par_index][child_index][2][i-1, :]

                    flag_sibling = False
                    for q_index in range(len(queue_tree[i])):
                        if (cur_index <= len(queue_tree[i])) and (q_index < cur_index - 1) and (queue_tree[i][q_index]["parent"] == queue_tree[i][cur_index - 1]["parent"]) and (queue_tree[i][q_index]["child_index"] < queue_tree[i][cur_index - 1]["child_index"]):
                            flag_sibling = True
                            sibling_index = q_index
                    if flag_sibling:
                        sibling_state[i - 1, :] = dec_state[sibling_index][dec_batch[sibling_index].size(
                            1) - 1][2][i - 1, :]

            parent_h = dec_state[cur_index][0][2]
            for i in range(dec_batch[cur_index].size(1) - 1):
                teacher_force = random.random() < self.teacher_force_ratio
                if teacher_force != True and i > 0:
                    input_word = pred.argmax(1)
                else:
                    input_word = dec_batch[cur_index][:, i]
                pred, rnn_state_iter, attn_scores = self.decode_step(tgt_batch_size=tgt_batch_size,
                                                                     dec_single_input=input_word,
                                                                     dec_single_state=(
                                                                         dec_state[cur_index][i][1], dec_state[cur_index][i][2]),
                                                                     memory=enc_outputs,
                                                                     parent_state=parent_h,
                                                                     oov_dict=oov_dict,
                                                                     enc_batch=enc_batch)

                dec_state[cur_index][i +
                                     1][1], dec_state[cur_index][i+1][2] = rnn_state_iter

                pred = torch.log(pred + 1e-31)
                loss += self.criterion(pred, dec_batch[cur_index][:, i+1])
            cur_index = cur_index + 1
        loss = loss / tgt_batch_size
        return loss

    def _filter_oov(self, tokens, vocab):
        r"""The function used to mask some oov word in word embedding layer."""
        ret = tokens.clone()
        ret[tokens >= vocab.vocab_size] = vocab.get_symbol_idx(vocab.unk_token)
        return ret

    def decode_step(self, tgt_batch_size, 
                          dec_single_input, 
                          dec_single_state, 
                          memory, 
                          parent_state, 
                          input_mask=None, 
                          memory_mask=None, 
                          memory_candidate=None, 
                          sibling_state=None, 
                          oov_dict=None, 
                          enc_batch=None):
        """The decoding function in tree decoder.

        Parameters
        ----------
        tgt_batch_size : int,
            batch size.
        dec_single_input : torch.Tensor,
            word id matrix for decoder input: [B, N].
        dec_single_state : torch.Tensor
            the rnn decoding hidden state: [B, N, D].
        memory : torch.Tensor
            the encoder output node embedding.
        parent_state : torch.Tensor
            the parent embedding used in parent feeding mechanism.
        input_mask : torch.Tensor, optional
            input mask, by default None
        memory_mask : torch.Tensor, optional
            mask for encoder output, by default None
        memory_candidate : torch.Tensor, optional
            encoder output used for separate attention mechanism, by default None
        sibling_state : torch.Tensor, optional
            sibling state for sibling feeding mechanism, by default None
        oov_dict : object, optional
            out-of-vocabulary object for copy mechanism, by default None
        enc_batch : torch.Tensor,
            The input batch : (Batch_size * Source sentence word index tensor).
        """
        dec_single_input = self._filter_oov(dec_single_input, self.tgt_vocab)
        rnn_state_c, rnn_state_h, dec_emb = self.rnn(
            dec_single_input, dec_single_state[0], dec_single_state[1], parent_state, sibling_state)
        
        attn_collect = []
        score_collect = []

        if self.separate_attn:
            pass
        else:
            context_vector, attn_scores = self.attention(
                query=rnn_state_h, memory=memory)
            attn_collect.append(context_vector)
            score_collect.append(attn_scores)

        pred = F.tanh(self.linear_att(
            torch.cat((context_vector, rnn_state_h), 1)))
        decoder_output = self.linear_out(self.dropout_attn(pred))
        if self.use_copy:
            assert enc_batch is not None
            assert oov_dict is not None
            output = torch.zeros(tgt_batch_size, oov_dict.vocab_size).to(self.device)
            attn_ptr = torch.cat(attn_collect, dim=-1)

            pgen_collect = [dec_emb, torch.cat((rnn_state_c, rnn_state_h), -1), attn_ptr]
            prob_ptr = torch.sigmoid(self.ptr(torch.cat(pgen_collect, -1)))
            prob_gen = 1 - prob_ptr
            gen_output = torch.softmax(decoder_output, dim=-1)

            ret = prob_gen * gen_output
            need_pad_length = len(oov_dict) - len(self.tgt_vocab)
            output = torch.cat((ret, ret.new_zeros((tgt_batch_size, need_pad_length))), dim=1)
            # output[:, :self.tgt_vocab.vocab_size] = ret

            ptr_output = attn_scores
            output.scatter_add_(1, enc_batch, prob_ptr * ptr_output)
            decoder_output = output
            # decoder_output = -F.threshold(-output, -1.0, -1.0)
        else:
            decoder_output = torch.softmax(decoder_output, dim=-1)

        return decoder_output, (rnn_state_c, rnn_state_h), attn_scores

    def translate(self, use_copy,
                  enc_hidden_size,
                  dec_hidden_size,
                  model,
                  input_graph_list,
                  word_manager,
                  form_manager,
                  device,
                  max_dec_seq_length,
                  max_dec_tree_depth,
                  use_beam_search=True,
                  beam_size=4,
                  oov_dict=None):
        input_graph_list.batch_node_features["token_id"] = input_graph_list.batch_node_features["token_id"].to(device)
        prev_c = torch.zeros((1, dec_hidden_size), requires_grad=False)
        prev_h = torch.zeros((1, dec_hidden_size), requires_grad=False)

        batch_graph = model.graph_topology(input_graph_list)
        batch_graph = model.gnn_encoder(batch_graph)
        batch_graph.node_features["rnn_emb"] = batch_graph.node_features['node_feat']

        params = model.decoder._extract_params(batch_graph)
        graph_node_embedding = params['graph_node_embedding']
        if model.decoder.graph_pooling_strategy == "max":
            graph_level_embedding = torch.max(graph_node_embedding, 1)[0]
        rnn_node_embedding = params['rnn_node_embedding']
        graph_node_mask = params['graph_node_mask']
        enc_w_list = params['enc_batch']

        enc_outputs = graph_node_embedding
        prev_c = graph_level_embedding
        prev_h = graph_level_embedding

        # decode
        queue_decode = []
        queue_decode.append(
            {"s": (prev_c, prev_h), "parent": 0, "child_index": 1, "t": Tree()})
        head = 1
        while head <= len(queue_decode) and head <= max_dec_tree_depth:
            s = queue_decode[head-1]["s"]
            parent_h = s[1]
            t = queue_decode[head-1]["t"]

            sibling_state = torch.zeros(
                (1, dec_hidden_size), dtype=torch.float, requires_grad=False)
            sibling_state = to_cuda(sibling_state, device)

            flag_sibling = False
            for q_index in range(len(queue_decode)):
                if (head <= len(queue_decode)) and (q_index < head - 1) and (queue_decode[q_index]["parent"] == queue_decode[head - 1]["parent"]) and (queue_decode[q_index]["child_index"] < queue_decode[head - 1]["child_index"]):
                    flag_sibling = True
                    sibling_index = q_index
            if flag_sibling:
                sibling_state = queue_decode[sibling_index]["s"][1]

            if head == 1:
                prev_word = torch.tensor([form_manager.get_symbol_idx(
                    form_manager.start_token)], dtype=torch.long)
            else:
                prev_word = torch.tensor(
                    [form_manager.get_symbol_idx('(')], dtype=torch.long)

            prev_word = to_cuda(prev_word, device)
            i_child = 1
            if not use_beam_search:
                while True:
                    prediction, (curr_c, curr_h), _ = model.decoder.decode_step(tgt_batch_size=1,
                                                                 dec_single_input=prev_word,
                                                                 dec_single_state=s,
                                                                 memory=enc_outputs,
                                                                 parent_state=parent_h,
                                                                 oov_dict=oov_dict,
                                                                 enc_batch=enc_w_list)
                    s = (curr_c, curr_h)
                    prev_word = torch.log(prediction + 1e-31)
                    prev_word = prev_word.argmax(1)

                    if int(prev_word[0]) == form_manager.get_symbol_idx(form_manager.end_token) or t.num_children >= max_dec_seq_length:
                        break
                    elif int(prev_word[0]) == form_manager.get_symbol_idx(form_manager.non_terminal_token):
                        queue_decode.append({"s": (s[0].clone(), s[1].clone()), "parent": head, "child_index": i_child, "t": Tree()})
                        t.add_child(int(prev_word[0]))
                    else:
                        t.add_child(int(prev_word[0]))
                    i_child = i_child + 1
            else:
                topk = 1
                # decoding goes sentence by sentence
                assert(graph_node_embedding.size(0) == 1)
                beam_search_generator = DecoderStrategy(
                    beam_size=beam_size, vocab=form_manager, decoder=model.decoder, rnn_type="lstm", use_copy=True, use_coverage=False)
                decoded_results = beam_search_generator.beam_search_for_tree_decoding(decoder_initial_state=(s[0], s[1]),
                                                                                          decoder_initial_input=prev_word,
                                                                                          parent_state=parent_h,
                                                                                          graph_node_embedding=enc_outputs,
                                                                                          rnn_node_embedding=rnn_node_embedding,
                                                                                          device=device,
                                                                                          topk=topk,
                                                                                          oov_dict=oov_dict,
                                                                                          enc_batch=enc_w_list)
                generated_sentence = decoded_results[0][0]
                for node_i in generated_sentence:
                    if int(node_i.wordid.item()) == form_manager.get_symbol_idx(form_manager.non_terminal_token):
                        queue_decode.append({"s": (node_i.h[0].clone(), node_i.h[1].clone(
                        )), "parent": head, "child_index": i_child, "t": Tree()})
                        t.add_child(int(node_i.wordid.item()))
                        i_child = i_child + 1
                    elif int(node_i.wordid.item()) != form_manager.get_symbol_idx(form_manager.end_token) and \
                            int(node_i.wordid.item()) != form_manager.get_symbol_idx(form_manager.start_token) and \
                            int(node_i.wordid.item()) != form_manager.get_symbol_idx('('):
                        t.add_child(int(node_i.wordid.item()))
                        i_child = i_child + 1

            head = head + 1
        for i in range(len(queue_decode)-1, 0, -1):
            cur = queue_decode[i]
            queue_decode[cur["parent"] -
                         1]["t"].children[cur["child_index"]-1] = cur["t"]
        return queue_decode[0]["t"].to_list(form_manager)

    def _build_rnn(self, rnn_type, input_size, emb_size, hidden_size, dropout_input, use_sibling):
        """_build_rnn : how the rnn unit should be build.
        """
        rnn = TreeDecodingUnit(input_size, emb_size, hidden_size, dropout_input, use_sibling, self.embeddings)

        return rnn

    def forward(self, g, tgt_tree_batch=None, oov_dict=None):
        params = self._extract_params(g)
        params['tgt_tree_batch'] = tgt_tree_batch
        params["oov_dict"] = oov_dict
        return self._run_forward_pass(**params)

    def _extract_params(self, graph_list):
        """

        Parameters
        ----------
        g: GraphData

        Returns
        -------
        params: dict
        """
        batch_data_dict = graph_list.batch_node_features
        graph_node_emb = batch_data_dict["node_emb"]
        
        # [s_g.node_features["node_emb"] for s_g in graph_list]
        rnn_node_emb = batch_data_dict["rnn_emb"]

        graph_node_mask = (batch_data_dict["token_id"] != 0).squeeze(-1).float() - 1

        if self.use_copy:
            src_seq_ret = graph_list.batch_node_features["token_id_oov"]
        else:
            src_seq_ret = None

        return {
            "graph_node_embedding": graph_node_emb,
            "graph_node_mask": graph_node_mask,
            "rnn_node_embedding": rnn_node_emb,
            "graph_level_embedding": None,
            "graph_edge_embedding": None,
            "graph_edge_mask": None,
            "enc_batch": src_seq_ret.long() if self.use_copy else None
        }

def create_mask(x, N, device=None):
    x = x.data
    mask = np.zeros((x.size(0), N))
    for i in range(x.size(0)):
        mask[i, :x[i]] = 1
    return torch.Tensor(mask).to(device)


class TreeDecodingUnit(nn.Module):
    def __init__(self, input_size, emb_size, hidden_size, dropout_input, use_sibling, dec_embeddings):
        super(TreeDecodingUnit, self).__init__()
        self.hidden_size = hidden_size
        self.emb_size = emb_size
        self.embedding = dec_embeddings
        self.dropout = nn.Dropout(dropout_input)

        self.lstm = nn.LSTMCell(
            emb_size + hidden_size * (2 if use_sibling else 1), hidden_size)
        self.use_sibling = use_sibling

    def forward(self, input_src, prev_c, prev_h, parent_h, sibling_state):

        src_emb = self.embedding(input_src)
        src_emb = self.dropout(src_emb)
        if self.use_sibling:
            input_single_step = torch.cat(
                (src_emb, parent_h, sibling_state), 1)
        else:
            input_single_step = torch.cat((src_emb, parent_h), 1)
        prev_cy, prev_hy = self.lstm(input_single_step, (prev_c, prev_h))
        return prev_cy, prev_hy, input_single_step


def get_dec_batch(dec_tree_batch, batch_size, device, form_manager):
    queue_tree = {}
    for i in range(1, batch_size+1):
        queue_tree[i] = []
        queue_tree[i].append(
            {"tree": dec_tree_batch[i-1], "parent": 0, "child_index": 1})

    cur_index, max_index = 1, 1
    dec_batch = {}
    # max_index: the max number of sequence decoder in one batch
    while (cur_index <= max_index):
        max_w_len = -1
        batch_w_list = []
        for i in range(1, batch_size+1):
            w_list = []
            if (cur_index <= len(queue_tree[i])):
                t = queue_tree[i][cur_index - 1]["tree"]

                for ic in range(t.num_children):
                    if isinstance(t.children[ic], Tree):
                        w_list.append(4)
                        queue_tree[i].append(
                            {"tree": t.children[ic], "parent": cur_index, "child_index": ic + 1})
                    else:
                        w_list.append(t.children[ic])
                if len(queue_tree[i]) > max_index:
                    max_index = len(queue_tree[i])
            if len(w_list) > max_w_len:
                max_w_len = len(w_list)
            batch_w_list.append(w_list)
        dec_batch[cur_index] = torch.zeros(
            (batch_size, max_w_len + 2), dtype=torch.long)
        for i in range(batch_size):
            w_list = batch_w_list[i]
            if len(w_list) > 0:
                for j in range(len(w_list)):
                    dec_batch[cur_index][i][j+1] = w_list[j]
                # add <S>, <E>
                if cur_index == 1:
                    dec_batch[cur_index][i][0] = 1
                else:
                    dec_batch[cur_index][i][0] = form_manager.get_symbol_idx(
                        '(')
                dec_batch[cur_index][i][len(w_list) + 1] = 2

        dec_batch[cur_index] = to_cuda(dec_batch[cur_index], device)
        cur_index += 1

    return dec_batch, queue_tree, max_index