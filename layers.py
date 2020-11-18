import tensorflow as tf
import tensorflow.keras as keras


class DenseLayer(keras.layers.Layer):
    def __init__(self, units, activation="elu", dropout_prob=0.1, kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(DenseLayer, self).__init__()
        self.layers = keras.Sequential()
        self.dropout_prob = dropout_prob
        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer
        self.units = units
        self.activation = activation

    def build(self, input_shape):
        for unit in self.units:
            self.layers.add(keras.layers.Dropout(self.dropout_prob))
            self.layers.add(keras.layers.Dense(units=unit, activation=self.activation,
                                               kernel_initializer=self.kernel_initializer,
                                               bias_initializer=self.bias_initializer))
            self.layers.add(keras.layers.LayerNormalization())

    def call(self, inputs):
        o = self.layers(inputs)
        return o


class ResidualLayer(keras.layers.Layer):
    def __init__(self, unit1s, unit2s=None, activation="elu", dropout_prob=0.1, kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(ResidualLayer, self).__init__()
        self.layer1 = keras.Sequential()
        self.unit1s = unit1s
        if unit2s is not None:
            if len(unit2s) == 0:
                raise Exception("unit2s should be None or a non-empty list")
            self.layer2 = keras.Sequential()
            self.unit2s = unit2s
        if len(self.unit1s) == 0:
            raise Exception("unit1s should not be empty")
        self.dropout_prob = dropout_prob
        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer
        self.activation = keras.activations.get(activation)

    def build(self, input_shape):
        for unit in self.unit1s:
            self.layer1.add(keras.layers.Dropout(self.dropout_prob))
            self.layer1.add(keras.layers.Dense(units=unit, activation=self.activation,
                                               kernel_initializer=self.kernel_initializer,
                                               bias_initializer=self.bias_initializer))
            self.layer1.add(keras.layers.LayerNormalization())
        if self.unit2s is not None:
            for unit in self.unit2s:
                self.layer2.add(keras.layers.Dropout(self.dropout_prob))
                self.layer2.add(keras.layers.Dense(units=unit, activation=self.activation,
                                                   kernel_initializer=self.kernel_initializer,
                                                   bias_initializer=self.bias_initializer))
                self.layer2.add(keras.layers.LayerNormalization())
            if self.unit2s[-1] != self.unit1s[-1]:
                self.layer = keras.layers.Dense(units=self.unit1s[-1])
        else:
            if input_shape[-1] != self.unit1s[-1]:
                self.layer = keras.layers.Dense(units=input_shape[-1])
        self.norm = keras.layers.LayerNormalization()
        self.built = True

    def call(self, inputs):
        x = self.layer1(inputs)
        if self.unit2s is not None:
            y = self.layer2(inputs)
            if self.layer is not None:
                y = self.layer(y)
            outputs = self.activation(x + y)
        else:
            if self.layer is not None:
                outputs = self.activation(x + self.layer(inputs))
            else:
                outputs = self.activation(x + inputs)

        outputs = self.norm(outputs)
        return outputs


class GraphAttention(keras.layers.Layer):
    def __init__(self, feature_size, attn_heads=8, dropout_prob=0.3, activation="elu",
                 attn_heads_reduction='mean', kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(GraphAttention, self).__init__()
        self.feature_size = feature_size
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
            kernel = self.add_weight(shape=(input_feature_size, self.feature_size), initializer=self.kernel_initializer)
            self.kernels.append(kernel)
            bias = self.add_weight(shape=(self.feature_size,), initializer=self.bias_initializer)
            self.biases.append(bias)
            attn_kernel_self = self.add_weight(shape=(self.feature_size, 1), initializer=self.kernel_initializer)
            attn_kernel_neighs = self.add_weight(shape=(self.feature_size, 1), initializer=self.kernel_initializer)
            self.attn_kernels.append([attn_kernel_self, attn_kernel_neighs])

        self.built = True

    def call(self, inputs):  # X, A
        X = inputs[0]
        A = inputs[1]
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

    def call(self, inputs):  # X, A
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

    def call(self, inputs):
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
        self.kernel = self.add_weight(shape=(i1, i2, self.unit), initializer=self.kernel_initializer)
        if self.use_bias:
            self.bias = self.add_weight(shape=(self.unit,), initializer=self.bias_initializer)
        self.built = True

    def call(self, inputs):
        i1 = inputs[0]  # batch, embed_size
        i2 = inputs[1]  # batch, embed_size
        output = tf.einsum("b...i,b...j,ijk->b...k", i1, i2, self.kernel)
        if self.use_bias:
            output = tf.nn.bias_add(output, self.bias)
        output = self.activation(output)
        return output
    
    
class MeanAggregator(keras.layers.Layer):
    def __init__(self, unit, activation='elu', concat=False, dropout_prob=0.3, use_bias=True,
                 kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(MeanAggregator, self).__init__()
        self.unit = unit
        self.activation = keras.activations.get(activation)
        self.concat = concat
        self.dropout_prob = dropout_prob
        self.use_bias = use_bias

        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):
        self_unit = input_shape[0][-1]
        neigh_unit = input_shape[1][-1]
        self.neigh_weights = self.add_weight(shape=(neigh_unit, self.unit), initializer=self.kernel_initializer)
        self.self_weights = self.add_weight(shape=(self_unit, self.unit), initializer=self.kernel_initializer)
        if self.use_bias:
            if self.concat:
                self.biases = self.add_weight(shape=(2 * self.unit,), initializer=self.bias_initializer)
            else:
                self.biases = self.add_weight(shape=(self.unit, ), initializer=self.bias_initializer)
        self.built = True

    def call(self, inputs):
        self_vec = inputs[0]  # batch, embed_size
        neigh_vec = inputs[1]  # batch, neigh_samples, embed_size
        self_vec = keras.layers.Dropout(self.dropout_prob)(self_vec)
        neigh_vec = keras.layers.Dropout(self.dropout_prob)(neigh_vec)

        neigh_means = tf.reduce_mean(neigh_vec, axis=1)  # batch, embed_size
        from_neighs = tf.matmul(neigh_means, self.neigh_weights)  # batch, unit
        from_self = tf.matmul(self_vec, self.self_weights)  # batch, unit

        if not self.concat:
            output = tf.add_n([from_self, from_neighs])
        else:
            output = tf.concat([from_self, from_neighs], axis=-1)

        if self.use_bias:
            output += self.biases

        return self.activation(output)

    def compute_output_shape(self, input_shape):
        if self.concat:
            return (None, self.unit * 2)
        else:
            return (None, self.unit)


class LSTMAggregator(keras.layers.Layer):
    def __init__(self, unit, activation='elu', concat=False, dropout_prob=0.3, use_bias=True,
                 kernel_initializer='glorot_uniform',
                 bias_initializer='zeros'):
        super(LSTMAggregator, self).__init__()
        self.unit = unit
        self.activation = keras.activations.get(activation)
        self.concat = concat
        self.dropout_prob = dropout_prob
        self.use_bias = use_bias

        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):
        self_unit = input_shape[0][-1]
        neigh_unit = input_shape[1][-1]
        self.neigh_weights = self.add_weight(shape=(neigh_unit, self.unit), initializer=self.kernel_initializer)
        self.self_weights = self.add_weight(shape=(self_unit, self.unit), initializer=self.kernel_initializer)
        if self.use_bias:
            if self.concat:
                self.biases = self.add_weight(shape=(2 * self.unit,), initializer=self.bias_initializer)
            else:
                self.biases = self.add_weight(shape=(self.unit, ), initializer=self.bias_initializer)
        self.cell = keras.layers.LSTM(self.unit, dropout=self.dropout_prob)
        self.built = True

    def call(self, inputs):
        self_vec = inputs[0]  # batch, embed_size
        neigh_vec = inputs[1]  # batch, neigh_samples, embed_size
        self_vec = keras.layers.Dropout(self.dropout_prob)(self_vec)
        neigh_vec = keras.layers.Dropout(self.dropout_prob)(neigh_vec)

        rnn_outputs = self.cell(neigh_vec)  # batch, unit
        from_neighs = tf.matmul(rnn_outputs, self.neigh_weights)  # batch, unit
        from_self = tf.matmul(self_vec, self.self_weights)  # batch, unit

        if not self.concat:
            output = tf.add_n([from_self, from_neighs])
        else:
            output = tf.concat([from_self, from_neighs], axis=-1)

        if self.use_bias:
            output += self.biases

        return self.activation(output)

    def compute_output_shape(self, input_shape):
        if self.concat:
            return None, self.unit * 2
        else:
            return None, self.unit


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
    def __init__(self, units, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones', support=2, mode="gat"):
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
        self.support = support
        self.mode = mode

    def build(self, input_shape):  # node_size, seq_len, embed_size+node_size
        self.node_size = input_shape[0]
        self.embed_size = input_shape[1] - self.node_size
        if self.mode == "gat":
            self.model = GraphAttention(self.units, kernel_initializer=self.kernel_initializer,
                        dropout_prob=self.dropout_prob, bias_initializer=self.bias_initializer)
        else:
            self.model = GraphConvolution(self.units, kernel_initializer=self.kernel_initializer,
                        dropout_prob=self.dropout_prob, bias_initializer=self.bias_initializer)
            self.filter = GCNFilter(support=self.support)
        self.Wz = self.add_weight(shape=(self.units, self.units), initializer=self.kernel_initializer)
        self.Uz = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bz = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wr = self.add_weight(shape=(self.units, self.units), initializer=self.kernel_initializer)
        self.Ur = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.br = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)
        self.Wh = self.add_weight(shape=(self.units, self.units), initializer=self.kernel_initializer)
        self.Uh = self.add_weight(shape=(self.units, self.units), initializer=self.recurrent_initializer)
        self.bh = self.add_weight(shape=(self.units,), initializer=self.bias_initializer)

    def call(self, input_at_t, states_at_t):  # node_size, embed_size + node_size;node_size, units *seq_len;
        state_at_t = keras.layers.Dropout(self.dropout_prob)(states_at_t[0])  # node_size, units
        x_at_t = tf.slice(input_at_t, [0, 0], [self.node_size, self.embed_size])  # node_size, embed_size
        a_at_t = tf.slice(input_at_t, [0, self.embed_size], [self.node_size, self.node_size])  # node_size, node_size
        if self.mode == "gcn":
            a_at_t = self.filter(a_at_t)
        input_at_t = self.model([x_at_t, a_at_t])
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


class GCRN2Cell(keras.layers.AbstractRNNCell):
    def __init__(self, units, activation="tanh", recurrent_activation="sigmoid",
                 use_bias=True, dropout_prob=0.3, recurrent_dropout_prob=0.3,
                 kernel_initializer='glorot_uniform', recurrent_initializer="orthogonal",
                 bias_initializer='ones', mode="gat",support=2):
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
        self.mode = mode  # "gat","gcn"
        self.support = support

    def build(self, input_shape):  # node_size, seq_len, embed_size + node_size
        self.node_size = input_shape[0]
        self.embed_size = input_shape[1] - self.node_size
        if self.mode == "gat":
            model = GraphAttention
        else:
            model = GraphConvolution
            self.filter = GCNFilter(support=self.support)
        self.Wz = model(self.units, kernel_initializer=self.kernel_initializer,
                        dropout_prob=self.dropout_prob, bias_initializer=self.bias_initializer)
        self.Uz = model(self.units, kernel_initializer=self.recurrent_initializer,
                        dropout_prob=self.recurrent_dropout_prob,
                        bias_initializer=self.bias_initializer)
        self.Wr = model(self.units, kernel_initializer=self.kernel_initializer,
                        dropout_prob=self.dropout_prob, bias_initializer=self.bias_initializer)
        self.Ur = model(self.units, kernel_initializer=self.recurrent_initializer,
                        dropout_prob=self.recurrent_dropout_prob,
                        bias_initializer=self.bias_initializer)
        self.Wh = model(self.units, kernel_initializer=self.kernel_initializer,
                        dropout_prob=self.dropout_prob, bias_initializer=self.bias_initializer)
        self.Uh = model(self.units, kernel_initializer=self.recurrent_initializer,
                        dropout_prob=self.recurrent_dropout_prob,
                        bias_initializer=self.bias_initializer)

    def call(self, input_at_t, states_at_t):  # node_size, embed_size + node_size;node_size, units *seq_len;
        state_at_t = keras.layers.Dropout(self.dropout_prob)(states_at_t[0])  # node_size, units
        x_at_t = tf.slice(input_at_t, [0, 0], [self.node_size, self.embed_size])  # node_size, embed_size
        a_at_t = tf.slice(input_at_t, [0, self.embed_size], [self.node_size, self.node_size])  # node_size, node_size
        if self.mode == "gcn":
            a_at_t = self.filter(a_at_t)
        x_at_t = keras.layers.Dropout(self.dropout_prob)(x_at_t)
        zt = self.recurrent_activation(self.Wz([x_at_t, a_at_t]) + self.Uz([state_at_t, a_at_t]))
        zt = keras.layers.Dropout(self.recurrent_dropout_prob)(zt)  # node_size, units

        rt = self.recurrent_activation(self.Wr([x_at_t, a_at_t]) + self.Ur([state_at_t, a_at_t]))
        rt = keras.layers.Dropout(self.recurrent_dropout_prob)(rt)  # node_size, units

        ht_ = self.recurrent_activation(self.Wh([x_at_t, a_at_t]) + self.Uh([state_at_t * rt, a_at_t]))
        ht_ = keras.layers.Dropout(self.recurrent_dropout_prob)(ht_)  # node_size, units

        ht = (1 - zt) * state_at_t + zt * ht_
        ht = keras.layers.Dropout(self.dropout_prob)(ht)
        return ht, ht

    @property
    def state_size(self):
        return self.units

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units
