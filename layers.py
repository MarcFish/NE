import tensorflow as tf
import tensorflow.keras as keras
import typing


class SampleSoftmaxLoss(keras.layers.Layer):
    def __init__(self, node_size, num_sampled=5, **kwargs):
        super(SampleSoftmaxLoss, self).__init__(**kwargs)
        self.node_size = node_size
        self.num_sampled = num_sampled

    def build(self, input_shape):  # y_true, embed
        units = input_shape[-1][-1]
        self.w = self.add_weight(shape=(self.node_size, units))
        self.b = self.add_weight(shape=(self.node_size, ))
        self.built = True

    def call(self, inputs, **kwargs):
        labels, embed = inputs
        if labels.shape.rank == 1:
            labels = tf.expand_dims(labels, axis=-1)
        loss = tf.reduce_mean(tf.nn.sampled_softmax_loss(weights=self.w,
                                                 biases=self.b,
                                                 inputs=embed,
                                                 labels=labels,
                                                 num_sampled=self.num_sampled,
                                                 num_classes=self.node_size))
        self.add_loss(loss)
        return embed


class GraphAttention(keras.layers.Layer):
    def __init__(self, units, attn_heads=8, dropout_prob=0.3, activation="elu",
                 attn_heads_reduction='mean', kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(GraphAttention, self).__init__()
        self.units = units
        self.attn_heads = attn_heads
        self.activation = keras.activations.get(activation)
        self.dropout_prob = dropout_prob

        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer

        self.kernels = list()
        self.biases = list()
        self.attn_kernels = list()

        self.attn_heads_reduction = attn_heads_reduction

    def build(self, input_shape):  # X, A
        input_feature_size = input_shape[0][-1]
        for head in range(self.attn_heads):
            kernel = self.add_weight(shape=(input_feature_size, self.units), initializer=self.kernel_initializer)
            self.kernels.append(kernel)
            bias = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
            self.biases.append(bias)
            attn_kernel_self = self.add_weight(shape=(self.units, 1), initializer=self.kernel_initializer)
            attn_kernel_neighs = self.add_weight(shape=(self.units, 1), initializer=self.kernel_initializer)
            self.attn_kernels.append([attn_kernel_self, attn_kernel_neighs])

        self.built = True

    def call(self, inputs, **kwargs):
        X = inputs[0]
        A = tf.cast(inputs[1], tf.float32)
        outputs = list()

        for head in range(self.attn_heads):
            kernel = self.kernels[head]
            attention_kernel = self.attn_kernels[head]

            X = keras.layers.Dropout(self.dropout_prob)(X)
            features = tf.matmul(X, kernel)

            attn_for_self = tf.matmul(features, attention_kernel[0])
            attn_for_neighs = tf.matmul(features, attention_kernel[1])

            dense = attn_for_self + tf.transpose(attn_for_neighs)
            dense = keras.layers.LeakyReLU(0.2)(dense)
            mask = (1.0 - A) * (-10e9)
            dense += mask
            dense = tf.nn.softmax(dense)

            dropout_attn = tf.keras.layers.Dropout(self.dropout_prob)(dense)
            dropout_feat = tf.keras.layers.Dropout(self.dropout_prob)(features)

            node_features = tf.matmul(dropout_attn, dropout_feat)

            node_features = tf.nn.bias_add(node_features, self.biases[head])

            # Add output of attention head to final output
            output = self.activation(node_features)
            outputs.append(output)

        if self.attn_heads_reduction == 'concat':
            output = tf.concat(outputs, axis=-1)
        else:
            output = tf.reduce_mean(tf.stack(outputs), axis=0)
        return output

    def compute_output_shape(self, input_shape):
        if self.attn_heads_reduction == "concat":
            return input_shape[0][0], self.units * self.attn_heads
        else:
            return input_shape[0][0], self.units


class GraphConvolution(keras.layers.Layer):
    """Basic graph convolution layer as in https://arxiv.org/abs/1609.02907"""
    def __init__(self, units, activation='relu', use_bias=True, kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(GraphConvolution, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.use_bias = use_bias

        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shapes):  # X, A
        features_shape = input_shapes[0]
        adjoint_shape = input_shapes[1]  # support, node_size, node_size
        assert len(features_shape) == 2
        input_dim = features_shape[1]
        support = adjoint_shape[0]
        self.support = support
        self.kernel = self.add_weight(shape=(input_dim * support, self.units), initializer=self.kernel_initializer)
        if self.use_bias:
            self.bias = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.built = True

    def call(self, inputs, **kwargs):
        features = inputs[0]  # node_size, feautre_size
        basis = inputs[1]  # support, node_size, node_size

        output = tf.matmul(basis, features)  # support, node_size, feature_size
        os = list()
        for i in range(self.support):
            os.append(output[i])
        output = tf.concat(os, axis=1)
        output = tf.matmul(output, self.kernel)
        if self.use_bias:
            output = tf.nn.bias_add(output, self.bias)
        return self.activation(output)


class GCNFilter(keras.layers.Layer):
    def __init__(self, mode="localpool", support=1):
        super(GCNFilter, self).__init__()
        self.support = support
        if mode == "localpool":
            self.process = self._localpool
            assert support >= 1
        else:
            self.process = self._chebyshev
            assert support >= 2

    def build(self, input_shapes):
        self.shape = input_shapes[1]
        self.built = True

    def call(self, inputs, **kwargs):
        return self.process(inputs)

    def _localpool(self, inputs):
        d = tf.linalg.diag(tf.pow(tf.reduce_sum(inputs, axis=-1), -0.5))
        out = tf.matmul(tf.transpose(tf.matmul(inputs, d)), d)
        out = tf.stack([out])
        return out

    def _chebyshev(self, inputs):
        d = tf.linalg.diag(tf.pow(tf.reduce_sum(inputs, axis=-1), -0.5))
        adj_norm = tf.matmul(tf.transpose(tf.matmul(inputs, d)), d)
        laplacian = tf.eye(self.shape) - adj_norm
        largest_eigval = tf.math.reduce_max(tf.linalg.eigvalsh(laplacian))
        scaled_laplacian = (2. / largest_eigval) * laplacian - tf.eye(self.shape)
        out = list()
        out.append(tf.eye(self.shape))
        out.append(scaled_laplacian)
        for i in range(2, self.support+1):
            o = 2 * tf.matmul(scaled_laplacian, out[-1]) - out[-2]
            out.append(o)
        out = tf.stack(out)
        return out


class Bilinear(keras.layers.Layer):
    def __init__(self, unit, activation='elu', use_bias=True, kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(Bilinear, self).__init__()
        self.unit = unit
        self.activation = keras.activations.get(activation)
        self.use_bias = use_bias

        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):
        assert len(input_shape) == 2
        i1 = input_shape[0][-1]
        i2 = input_shape[1][-1]
        self.kernel = self.add_weight(shape=(i1, i2, self.units), initializer=self.kernel_initializer)
        if self.use_bias:
            self.bias = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.built = True

    def call(self, inputs, **kwargs):
        i1 = inputs[0]  # batch, embed_size
        i2 = inputs[1]  # batch, embed_size
        output = tf.einsum("b...i,b...j,ijk->b...k", i1, i2, self.kernel)
        if self.use_bias:
            output = tf.nn.bias_add(output, self.bias)
        output = self.activation(output)
        return output

    def compute_output_shape(self, input_shape):
        return input_shape[0][0], self.units


class GraphSageConv(keras.layers.Layer):
    def __init__(self, units, activation='elu', agg="mean", concat=True, dropout_prob=0.3, use_bias=True,
                 kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(GraphSageConv, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.dropout_prob = dropout_prob
        self.use_bias = use_bias
        self.concat = concat
        if agg in ["mean", "pool"]:
            if agg == "mean":
                self.agg = self.mean
            elif agg == "pool":
                self.agg = self.pool
            else:
                raise Exception("UnSupport Method")
        else:
            raise Exception("UnSupport Method")

        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):
        self_unit = input_shape[0][-1]
        neigh_unit = input_shape[0][-1]
        self.neigh_weights = self.add_weight(shape=(neigh_unit, self.units), initializer=self.kernel_initializer)
        self.self_weights = self.add_weight(shape=(self_unit, self.units), initializer=self.kernel_initializer)
        if self.use_bias:
            if self.concat:
                self.biases = self.add_weight(shape=(2 * self.units,), initializer=self.bias_initializer)
            else:
                self.biases = self.add_weight(shape=(self.units, ), initializer=self.bias_initializer)

        if self.agg == self.pool:
            self.pool_w = self.add_weight(shape=(neigh_unit, self.units), initializer=self.kernel_initializer)
            self.pool_b = self.add_weight(shape=(neigh_unit, ), initializer=self.bias_initializer)
        self.built = True

    def call(self, inputs, **kwargs):
        x, a = inputs
        a = tf.sparse.from_dense(a)
        o = self.agg(x, a)
        o = tf.nn.dropout(o, self.dropout_prob)
        o = tf.math.l2_normalize(o, axis=-1)
        return o

    def pool(self, x, a: tf.SparseTensor):
        from_self = tf.matmul(x, self.self_weights)
        from_neighs = tf.math.unsorted_segment_max(tf.gather(x, a.indices[:, 0], axis=-2), a.indices[:, 1], x.shape[0])
        from_neighs = tf.matmul(from_neighs, self.neigh_weights)

        if not self.concat:
            output = tf.add_n([from_self, from_neighs])
        else:
            output = tf.concat([from_self, from_neighs], axis=-1)

        if self.use_bias:
            output += self.biases

        return self.activation(output)

    def mean(self, x, a: tf.SparseTensor):
        from_self = tf.matmul(x, self.self_weights)
        from_neighs = tf.math.unsorted_segment_mean(tf.gather(x, a.indices[:, 0], axis=-2), a.indices[:, 1], x.shape[0])
        from_neighs = tf.matmul(from_neighs, self.neigh_weights)

        if not self.concat:
            output = tf.add_n([from_self, from_neighs])
        else:
            output = tf.concat([from_self, from_neighs], axis=-1)

        if self.use_bias:
            output += self.biases

        return self.activation(output)

    def compute_output_shape(self, input_shape):
        if self.concat:
            return input_shape[0][0], self.units * 2
        else:
            return input_shape[0][0], self.units


class GRUCell(keras.layers.AbstractRNNCell):
    def __init__(self, units, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones',):
        super(GRUCell, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.recurrent_activation = keras.activations.get(recurrent_activation)
        self.use_bias = use_bias
        self.dropout_prob = dropout_prob
        self.recurrent_dropout_prob = recurrent_dropout_prob
        self.kernel_initializer = kernel_initializer
        self.recurrent_initializer = recurrent_initializer
        self.bias_initializer = bias_initializer
    
    def build(self, input_shape):
        self.Wz = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uz = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bz = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wr = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Ur = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.br = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wh = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uh = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bh = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)

    def call(self, input_at_t, states_at_t):  # batch, embed_size;batch, units *seq_len;
        state_at_t = keras.layers.Dropout(self.dropout_prob)(states_at_t[0])  # batch, units
        input_at_t = keras.layers.Dropout(self.dropout_prob)(input_at_t)
        zt = self.recurrent_activation(tf.matmul(input_at_t, self.Wz) + tf.matmul(state_at_t, self.Uz) + self.bz)
        zt = keras.layers.Dropout(self.recurrent_dropout_prob)(zt)

        rt = self.recurrent_activation(tf.matmul(input_at_t, self.Wr) + tf.matmul(state_at_t, self.Ur) + self.br)
        rt = keras.layers.Dropout(self.recurrent_dropout_prob)(rt)

        ht_ = self.recurrent_activation(tf.matmul(input_at_t, self.Wh) + tf.matmul(state_at_t * rt, self.Uh) + self.bh)
        ht_ = keras.layers.Dropout(self.recurrent_dropout_prob)(ht_)

        ht = (1 - zt) * state_at_t + zt * ht_
        ht = keras.layers.Dropout(self.dropout_prob)(ht)
        return ht, ht
    
    @property
    def state_size(self):
        return self.units

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units


class LSTMCell(keras.layers.AbstractRNNCell):
    def __init__(self, units, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones',):
        super(LSTMCell, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.recurrent_activation = keras.activations.get(recurrent_activation)
        self.use_bias = use_bias
        self.dropout_prob = dropout_prob
        self.recurrent_dropout_prob = recurrent_dropout_prob
        self.kernel_initializer = kernel_initializer
        self.recurrent_initializer = recurrent_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):  # batch, seq_len, embed_size
        self.batch = input_shape[0]
        self.embed_size = input_shape[1]

        self.Wf = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uf = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bf = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wi = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Ui = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bi = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wo = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uo = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bo = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wc = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uc = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bc = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)

    def call(self, input_at_t, states_at_t):  # batch, embed_size;batch, units *seq_len;
        state_at_t = states_at_t[0]  # batch, units * 2
        h_at_t = tf.slice(state_at_t, [0, 0], [self.batch, self.units])
        c_at_t = tf.slice(state_at_t, [0, self.units], [self.batch, self.units])
        h_at_t = keras.layers.Dropout(self.dropout_prob)(h_at_t)
        c_at_t = keras.layers.Dropout(self.dropout_prob)(c_at_t)
        input_at_t = keras.layers.Dropout(self.dropout_prob)(input_at_t)

        ft = self.recurrent_activation(tf.matmul(input_at_t, self.Wf) + tf.matmul(h_at_t, self.Uf) + self.bf)
        ft = keras.layers.Dropout(self.recurrent_dropout_prob)(ft)

        it = self.recurrent_activation(tf.matmul(input_at_t, self.Wi) + tf.matmul(h_at_t, self.Ui) + self.bi)
        it = keras.layers.Dropout(self.recurrent_dropout_prob)(it)

        ot = self.recurrent_activation(tf.matmul(input_at_t, self.Wo) + tf.matmul(h_at_t, self.Uo) + self.bo)
        ot = keras.layers.Dropout(self.recurrent_dropout_prob)(ot)

        ct_ = self.recurrent_activation(tf.matmul(input_at_t, self.Wc) + tf.matmul(h_at_t, self.Uc) + self.bc)
        ct_ = keras.layers.Dropout(self.recurrent_dropout_prob)(ct_)

        ct = ft * c_at_t + it * ct_
        ht = ot * self.activation(ct)
        return ht, tf.concat([ht, ct], axis=-1)

    @property
    def state_size(self):
        return self.units * 2

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units


class TLSTMCell(keras.layers.AbstractRNNCell):
    def __init__(self, units, delta_dim = 1, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones',):
        super(TLSTMCell, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.recurrent_activation = keras.activations.get(recurrent_activation)
        self.use_bias = use_bias
        self.dropout_prob = dropout_prob
        self.recurrent_dropout_prob = recurrent_dropout_prob
        self.kernel_initializer = kernel_initializer
        self.recurrent_initializer = recurrent_initializer
        self.bias_initializer = bias_initializer
        self.delta_dim = delta_dim

    def build(self, input_shape):  # batch, seq_len, embed_size+delta_dim
        self.batch = input_shape[0]
        self.embed_size = input_shape[1] - 1

        self.Wd = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_activation)
        self.bd = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)

        self.Wf = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uf = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bf = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wi = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Ui = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bi = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wo = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uo = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bo = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wc = self.add_weight(shape=(input_shape[-1], self.units), initializer=self.kernel_initializer)
        self.Uc = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bc = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)

    def call(self, input_at_t, states_at_t):  # batch, embed_size+delta_dim;batch, units *seq_len;
        state_at_t = states_at_t[0]  # batch, units * 2
        h_at_t = tf.slice(state_at_t, [0, 0], [self.batch, self.units])
        c_at_t = tf.slice(state_at_t, [0, self.units], [self.batch, self.units])
        h_at_t = keras.layers.Dropout(self.dropout_prob)(h_at_t)
        c_at_t = keras.layers.Dropout(self.dropout_prob)(c_at_t)
        delta_t = tf.slice(input_at_t, [0, self.embed_size], [self.batch, self.delta_dim])
        input_at_t = tf.slice([input_at_t], [0, 0], [self.batch, self.embed_size])
        input_at_t = keras.layers.Dropout(self.dropout_prob)(input_at_t)
        cs = self.activation(tf.matmul(c_at_t, self.Wd) + self.bd)  # batch, units
        cs_ = cs * (1 / tf.log(2.7183 + delta_t))
        ct = c_at_t - cs
        c_at_t = ct + cs_

        ft = self.recurrent_activation(tf.matmul(input_at_t, self.Wf) + tf.matmul(h_at_t, self.Uf) + self.bf)
        ft = keras.layers.Dropout(self.recurrent_dropout_prob)(ft)

        it = self.recurrent_activation(tf.matmul(input_at_t, self.Wi) + tf.matmul(h_at_t, self.Ui) + self.bi)
        it = keras.layers.Dropout(self.recurrent_dropout_prob)(it)

        ot = self.recurrent_activation(tf.matmul(input_at_t, self.Wo) + tf.matmul(h_at_t, self.Uo) + self.bo)
        ot = keras.layers.Dropout(self.recurrent_dropout_prob)(ot)

        ct_ = self.recurrent_activation(tf.matmul(input_at_t, self.Wc) + tf.matmul(h_at_t, self.Uc) + self.bc)
        ct_ = keras.layers.Dropout(self.recurrent_dropout_prob)(ct_)

        ct = ft * c_at_t + it * ct_
        ht = ot * self.activation(ct)
        return ht, tf.concat([ht, ct], axis=-1)

    @property
    def state_size(self):
        return self.units * 2

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units


class GCRN1Cell(keras.layers.AbstractRNNCell):
    def __init__(self, units, func_cls, func_kwargs, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones'):
        super(GCRN1Cell, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.recurrent_activation = keras.activations.get(recurrent_activation)
        self.use_bias = use_bias
        self.dropout_prob = dropout_prob
        self.recurrent_dropout_prob = recurrent_dropout_prob
        self.kernel_initializer = kernel_initializer
        self.recurrent_initializer = recurrent_initializer
        self.bias_initializer = bias_initializer
        self.func_cls = func_cls
        self.func_kwargs = func_kwargs

    def build(self, input_shape):  # node_size, seq_len, embed_size+node_size
        self.func = self.func_cls(**self.func_kwargs)
        self.Wz = self.add_weight(shape=(self.units, self.units), initializer=self.kernel_initializer)
        self.Uz = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bz = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wr = self.add_weight(shape=(self.units, self.units), initializer=self.kernel_initializer)
        self.Ur = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.br = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wh = self.add_weight(shape=(self.units, self.units), initializer=self.kernel_initializer)
        self.Uh = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bh = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)

    def call(self, input_at_t, states_at_t):  # node_size, embed_size; node_size, node_size * n
        state_at_t = states_at_t[0]
        input_at_t = self.func([state_at_t, input_at_t])
        zt = self.recurrent_activation(tf.matmul(input_at_t, self.Wz) + tf.matmul(state_at_t, self.Uz) + self.bz)
        zt = keras.layers.Dropout(self.recurrent_dropout_prob)(zt)

        rt = self.recurrent_activation(tf.matmul(input_at_t, self.Wr) + tf.matmul(state_at_t, self.Ur) + self.br)
        rt = keras.layers.Dropout(self.recurrent_dropout_prob)(rt)

        ht_ = self.recurrent_activation(tf.matmul(input_at_t, self.Wh) + tf.matmul(state_at_t * rt, self.Uh) + self.bh)
        ht_ = keras.layers.Dropout(self.recurrent_dropout_prob)(ht_)

        ht = (1 - zt) * state_at_t + zt * ht_
        ht = keras.layers.Dropout(self.dropout_prob)(ht)
        return ht, ht

    @property
    def state_size(self):
        return self.units

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units

    @property
    def output_size(self):
        return self.units


class GCRN2Cell(keras.layers.AbstractRNNCell):
    def __init__(self, units, func_cls, func_kwargs, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones'):
        super(GCRN2Cell, self).__init__()
        self.units = units
        self.activation = keras.activations.get(activation)
        self.recurrent_activation = keras.activations.get(recurrent_activation)
        self.use_bias = use_bias
        self.dropout_prob = dropout_prob
        self.recurrent_dropout_prob = recurrent_dropout_prob
        self.kernel_initializer = kernel_initializer
        self.recurrent_initializer = recurrent_initializer
        self.bias_initializer = bias_initializer
        self.func_cls = func_cls
        self.func_kwargs = func_kwargs

    def build(self, input_shape):  # node_size, embed_size;node_size, node_size;

        self.Wz = self.func_cls(**self.func_kwargs)
        self.Uz = self.func_cls(**self.func_kwargs)
        self.Wr = self.func_cls(**self.func_kwargs)
        self.Ur = self.func_cls(**self.func_kwargs)
        self.Wh = self.func_cls(**self.func_kwargs)
        self.Uh = self.func_cls(**self.func_kwargs)

    def call(self, input_at_t, states_at_t):
        state_at_t = states_at_t[0]
        zt = self.recurrent_activation(self.Wz([state_at_t, input_at_t]) + self.Uz([state_at_t, input_at_t]))
        zt = tf.nn.dropout(zt, self.recurrent_dropout_prob)  # node_size, units

        rt = self.recurrent_activation(self.Wr([state_at_t, input_at_t]) + self.Ur([state_at_t, input_at_t]))
        rt = tf.nn.dropout(rt, self.recurrent_dropout_prob)  # node_size, units

        ht_ = self.recurrent_activation(self.Wh([state_at_t, input_at_t]) + self.Uh([state_at_t * rt, input_at_t]))
        ht_ = tf.nn.dropout(ht_, self.recurrent_dropout_prob)  # node_size, units

        ht = (1 - zt) * state_at_t + zt * ht_
        ht = tf.nn.dropout(ht, self.dropout_prob)
        return ht, ht

    @property
    def state_size(self):
        return self.units

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units

    @property
    def output_size(self):
        return self.units
