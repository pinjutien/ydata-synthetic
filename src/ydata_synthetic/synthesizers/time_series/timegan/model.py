"""
TimeGAN class implemented accordingly with:
Original code can be found here: https://bitbucket.org/mvdschaar/mlforhealthlabpub/src/master/alg/timegan/
"""
from tensorflow import function, GradientTape, sqrt, abs, reduce_mean, ones_like, zeros_like, random, float32
from tensorflow import data as tfdata
from tensorflow import train as tftrain
from tensorflow import nn
from tensorflow.keras import Model, Sequential, Input
from tensorflow.keras.layers import GRU, LSTM, Dense, RNN, GRUCell
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.losses import BinaryCrossentropy, MeanSquaredError

from tqdm import tqdm

from ydata_synthetic.synthesizers import gan

def make_net(model, n_layers, hidden_units, output_units, net_type='GRU'):
    if net_type=='GRU':
        for i in range(n_layers):
            model.add(GRU(units=hidden_units,
                      return_sequences=True,
                      name=f'GRU_{i + 1}'))
    else:
        for i in range(n_layers):
            model.add(LSTM(units=hidden_units,
                      return_sequences=True,
                      name=f'LSTM_{i + 1}'))

    model.add(Dense(units=output_units,
                    activation='sigmoid',
                    name='OUT'))
    return model


class TimeGAN(gan.Model):
    def __init__(self, model_parameters, hidden_dim, seq_len, n_seq, gamma):
        self.seq_len=seq_len
        self.n_seq=n_seq
        self.hidden_dim=hidden_dim
        self.gamma=gamma
        super().__init__(model_parameters)

    def define_gan(self):
        self.generator_aux=Generator(self.hidden_dim).build(input_shape=(self.seq_len, self.n_seq))
        self.supervisor=Supervisor(self.hidden_dim).build(input_shape=(self.hidden_dim, self.hidden_dim))
        self.discriminator=Discriminator(self.hidden_dim).build(input_shape=(self.seq_len, self.n_seq))
        self.recovery = Recovery(self.hidden_dim, self.n_seq).build(input_shape=(self.hidden_dim, self.hidden_dim))
        self.embedder = Embedder(self.hidden_dim).build(input_shape=(self.hidden_dim, self.n_seq))

        X = Input(shape=[self.seq_len, self.n_seq], batch_size=self.batch_size, name='RealData')
        Z = Input(shape=[self.seq_len, self.n_seq], batch_size=self.batch_size, name='RandomNoise')

        #--------------------------------
        # Building the AutoEncoder
        #--------------------------------
        H = self.embedder(X)
        X_tilde = self.recovery(H)

        self.autoencoder = Model(inputs=X, outputs=X_tilde)

        #---------------------------------
        # Adversarial Supervise Architecture
        #---------------------------------
        E_Hat = self.generator_aux(Z)
        H_hat = self.supervisor(E_Hat)
        Y_fake = self.discriminator(H_hat)

        self.adversarial_supervised = Model(inputs=Z,
                                       outputs=Y_fake,
                                       name='AdversarialSupervised')

        #---------------------------------
        # Adversarial architecture in latent space
        #---------------------------------
        Y_fake_e = self.discriminator(E_Hat)

        self.adversarial_embedded = Model(inputs=Z,
                                    outputs=Y_fake_e,
                                    name='AdversarialEmbedded')
        # ---------------------------------
        # Synthetic data generation
        # ---------------------------------
        X_hat = self.recovery(H_hat)
        self.generator = Model(inputs=Z,
                            outputs=X_hat,
                            name='FinalGenerator')

        # --------------------------------
        # Final discriminator model
        # --------------------------------
        Y_real = self.discriminator(H)
        self.discriminator_model = Model(inputs=X,
                                         outputs=Y_real,
                                         name="RealDiscriminator")

        # ----------------------------
        # Init the optimizers
        # ----------------------------
        self.autoencoder_opt = Adam(learning_rate=self.lr)
        self.supervisor_opt = Adam(learning_rate=self.lr)
        self.generator_opt = Adam(learning_rate=self.lr)
        self.discriminator_opt = Adam(learning_rate=self.lr)
        self.embedding_opt = Adam(learning_rate=self.lr)

        # ----------------------------
        # Define the loss functions
        # ----------------------------
        self._mse=MeanSquaredError()
        self._bce=BinaryCrossentropy()


    @function
    def train_autoencoder(self, x):
        with GradientTape() as tape:
            x_tilde = self.autoencoder(x)
            embedding_loss_t0 = self._mse(x, x_tilde)
            e_loss_0 = 10 * sqrt(embedding_loss_t0)

        var_list = self.embedder.trainable_variables + self.recovery.trainable_variables
        gradients = tape.gradient(e_loss_0, var_list)
        self.autoencoder_opt.apply_gradients(zip(gradients, var_list))
        return sqrt(embedding_loss_t0)

    @function
    def train_supervisor(self, x):
        with GradientTape() as tape:
            h = self.embedder(x)
            h_hat_supervised = self.supervisor(h)
            g_loss_s = self._mse(h[:, 1:, :], h_hat_supervised[:, 1:, :])

        var_list = self.supervisor.trainable_variables
        gradients = tape.gradient(g_loss_s, var_list)
        self.supervisor_opt.apply_gradients(zip(gradients, var_list))
        return g_loss_s

    @function
    def train_embedder(self,x):
        with GradientTape() as tape:
            h = self.embedder(x)
            h_hat_supervised = self.supervisor(h)
            generator_loss_supervised = self._mse(h[:, 1:, :], h_hat_supervised[:, 1:, :])

            x_tilde = self.autoencoder(x)
            embedding_loss_t0 = self._mse(x, x_tilde)
            e_loss = 10 * sqrt(embedding_loss_t0) + 0.1 * generator_loss_supervised

        var_list = self.embedder.trainable_variables + self.recovery.trainable_variables
        gradients = tape.gradient(e_loss, var_list)
        self.embedding_opt.apply_gradients(zip(gradients, var_list))
        return sqrt(embedding_loss_t0)

    def discriminator_loss(self, x, z):
        y_real = self.discriminator_model(x)
        discriminator_loss_real = self._bce(y_true=ones_like(y_real),
                                            y_pred=y_real)

        y_fake = self.adversarial_supervised(z)
        discriminator_loss_fake = self._bce(y_true=zeros_like(y_fake),
                                            y_pred=y_fake)

        y_fake_e = self.adversarial_embedded(z)
        discriminator_loss_fake_e = self._bce(y_true=zeros_like(y_fake_e),
                                              y_pred=y_fake_e)
        return (discriminator_loss_real +
                discriminator_loss_fake +
                self.gamma * discriminator_loss_fake_e)

    @staticmethod
    def calc_generator_moments_loss(y_true, y_pred):
        y_true_mean, y_true_var = nn.moments(x=y_true, axes=[0])
        y_pred_mean, y_pred_var = nn.moments(x=y_pred, axes=[0])
        g_loss_mean = reduce_mean(abs(y_true_mean - y_pred_mean))
        g_loss_var = reduce_mean(abs(sqrt(y_true_var + 1e-6) - sqrt(y_pred_var + 1e-6)))
        return g_loss_mean + g_loss_var

    @function
    def train_generator(self, x, z):
        with GradientTape() as tape:
            y_fake = self.adversarial_supervised(z)
            generator_loss_unsupervised = self._bce(y_true=ones_like(y_fake),
                                                    y_pred=y_fake)

            y_fake_e = self.adversarial_embedded(z)
            generator_loss_unsupervised_e = self._bce(y_true=ones_like(y_fake_e),
                                                      y_pred=y_fake_e)
            h = self.embedder(x)
            h_hat_supervised = self.supervisor(h)
            generator_loss_supervised = self._mse(h[:, 1:, :], h_hat_supervised[:, 1:, :])

            x_hat = self.generator(z)
            generator_moment_loss = self.calc_generator_moments_loss(x, x_hat)

            generator_loss = (generator_loss_unsupervised +
                              generator_loss_unsupervised_e +
                              100 * sqrt(generator_loss_supervised) +
                              100 * generator_moment_loss)

        var_list = self.generator_aux.trainable_variables + self.supervisor.trainable_variables
        gradients = tape.gradient(generator_loss, var_list)
        self.generator_opt.apply_gradients(zip(gradients, var_list))
        return generator_loss_unsupervised, generator_loss_supervised, generator_moment_loss

    @function
    def train_discriminator(self, x, z):
        with GradientTape() as tape:
            discriminator_loss = self.discriminator_loss(x, z)

        var_list = self.discriminator.trainable_variables
        gradients = tape.gradient(discriminator_loss, var_list)
        self.discriminator_opt.apply_gradients(zip(gradients, var_list))
        return discriminator_loss

    def get_batch_data(self, data, n_windows):
        return iter(tfdata.Dataset.from_tensor_slices(data)
                                .shuffle(buffer_size=n_windows)
                                .batch(self.batch_size).repeat())

    def _generate_noise(self):
        while True:
            yield np.random.uniform(low=0, high=1, size=(self.seq_len, self.n_seq))

    def get_batch_noise(self):
        return iter(tfdata.Dataset.from_generator(self._generate_noise, output_types=float32)
                                .batch(self.batch_size)
                                .repeat())

    def train(self, data, train_steps):
        step_g_loss_u = step_g_loss_s = step_g_loss_v = step_e_loss_t0 = step_d_loss = 0
        for step in tqdm(range(train_steps)):

            #Train the generator (k times as often as the discriminator)
            # Here k=2
            for _ in range(2):
                X_ = next(self.get_batch_data(data, n_windows=len(data)))
                Z_ = next(self.get_batch_noise())

                # --------------------------
                # Train the generator
                # --------------------------
                step_g_loss_u, step_g_loss_s, step_g_loss_v = self.train_generator(X_, Z_)

                # --------------------------
                # Train the embedder
                # --------------------------
                step_e_loss_t0 = self.train_embedder(X_)

            X_ = next(self.get_batch_data(data, n_windows=len(data)))
            Z_ = next(self.get_batch_noise())
            step_d_loss = self.discriminator_loss(X_, Z_)
            if step_d_loss > 0.15:
                step_d_loss = self.train_discriminator(X_, Z_)

            #Log here the results
            logging_hook = tftrain.LoggingTensorHook({"d_loss": step_d_loss,
                                                      "g_loss_u": step_g_loss_u,
                                                      "g_loss_v": step_g_loss_v,
                                                      "g_loss_s": step_g_loss_v,
                                                      "e_loss_t0": step_e_loss_t0}, every_n_iter=1000)


class Generator(Model):
    def __init__(self, hidden_dim, net_type='GRU'):
        self.hidden_dim = hidden_dim
        self.net_type = net_type

    def build(self, input_shape):
        model = Sequential(name='Generator')
        model.add(Input(shape=input_shape))
        model = make_net(model,
                         n_layers=3,
                         hidden_units=self.hidden_dim,
                         output_units=self.hidden_dim,
                         net_type=self.net_type)
        return model

class Discriminator(Model):
    def __init__(self, hidden_dim, net_type='GRU'):
        self.hidden_dim = hidden_dim
        self.net_type=net_type

    def build(self, input_shape):
        model = Sequential(name='Discriminator')
        model = make_net(model,
                         n_layers=3,
                         hidden_units=self.hidden_dim,
                         output_units=1,
                         net_type=self.net_type)
        return model

class Recovery(Model):
    def __init__(self, hidden_dim, n_seq):
        self.hidden_dim=hidden_dim
        self.n_seq=n_seq
        return

    def build(self, input_shape):
        recovery = Sequential(name='Recovery')
        recovery.add(Input(shape=input_shape, name='EmbeddedData'))
        recovery = make_net(recovery,
                            n_layers=3,
                            hidden_units=self.hidden_dim,
                            output_units=self.n_seq)
        return recovery

class Embedder(Model):

    def __init__(self, hidden_dim):
        self.hidden_dim=hidden_dim
        return

    def build(self, input_shape):
        embedder = Sequential(name='Embedder')
        embedder.add(Input(shape=input_shape, name='Data'))
        embedder = make_net(embedder,
                            n_layers=3,
                            hidden_units=self.hidden_dim,
                            output_units=self.hidden_dim)
        return embedder

class Supervisor(Model):
    def __init__(self, hidden_dim):
        self.hidden_dim=hidden_dim

    def build(self, input_shape):
        model = Sequential(name='Supervisor')
        model.add(Input(shape=input_shape))
        model = make_net(model,
                         n_layers=2,
                         hidden_units=self.hidden_dim,
                         output_units=self.hidden_dim)
        return model


if __name__ == '__main__':
    import pandas as pd
    import numpy as np
    import matplotlib.pyplot as plt
    from sklearn.preprocessing import MinMaxScaler
    """
    import quandl
    quandl_api_key = "ssf2fazqBjcLq-qhWTvo"
    quandl.ApiConfig.api_key=quandl_api_key
    quandl.ApiConfig.verify_ssl = False


    dataset = []
    for tick in tickers:
        dataset.append(quandl.get_table('WIKI/PRICES', ticker=tick))

    data = pd.concat(dataset)
    """
    tickers = ['BA', 'CAT', 'DIS', 'GE', 'IBM', 'KO']
    data = pd.read_csv('wiki_prices.csv')
    data=data.drop('None', axis=1)

    data = data.set_index(['ticker', 'date']).adj_close.unstack(level=0).loc['2000':, tickers].dropna()

    #Normalize the data
    scaler = MinMaxScaler()
    scaled_data = scaler.fit_transform(data).astype(np.float32)

    #Create rolling windows for the data
    seq_len=24
    n_seq=6

    dataset = []
    for i in range(len(data) - seq_len):
        dataset.append(scaled_data[i:i+seq_len])
    n_windows=len(dataset)

    noise_dim = 32
    dim = 128
    batch_size = 128

    log_step = 100
    epochs = 500 + 1
    learning_rate = 5e-4
    models_dir = './cache'

    gan_args = [batch_size, learning_rate, noise_dim, 24, 2, (0, 1), dim]

    synth = TimeGAN(model_parameters=gan_args, hidden_dim=24, seq_len=seq_len, n_seq=n_seq, gamma=1)
    synth.train(dataset, train_steps=10000)

    print('result')

