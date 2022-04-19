from glob import glob

from tensorflow.keras.optimizers import Adam
from tqdm import tqdm

from net.discriminator import Discriminator
from net.generator import Generator
from tools.data_loader import ImageGenerator
from tools.ops import *
from tools.utils import *


class AnimeGANv2(object):
    def __init__(self, args):
        self.model_name = 'AnimeGANv2'
        self.checkpoint_dir = args.checkpoint_dir
        self.log_dir = args.log_dir
        self.dataset_name = args.dataset

        self.epoch = args.epoch
        self.init_epoch = args.init_epoch  # args.epoch // 20

        self.gan_type = args.gan_type
        self.batch_size = args.batch_size
        self.save_freq = args.save_freq

        self.init_lr = args.init_lr
        self.d_lr = args.d_lr
        self.g_lr = args.g_lr

        """ Weight """
        self.g_adv_weight = args.g_adv_weight
        self.d_adv_weight = args.d_adv_weight
        self.con_weight = args.con_weight
        self.sty_weight = args.sty_weight
        self.color_weight = args.color_weight
        self.tv_weight = args.tv_weight

        self.training_rate = args.training_rate
        self.ld = args.ld

        self.img_size = args.img_size
        self.img_ch = args.img_ch

        """ Discriminator """
        self.n_dis = args.n_dis
        self.ch = args.ch
        self.sn = args.sn

        self.sample_dir = os.path.join(args.sample_dir, self.model_dir)
        check_folder(self.sample_dir)

        self.real_image_generator = ImageGenerator('./dataset/train_photo', self.img_size, self.batch_size)
        self.anime_image_generator = ImageGenerator('./dataset/{}'.format(self.dataset_name + '/style'), self.img_size,
                                                    self.batch_size)
        self.anime_smooth_generator = ImageGenerator('./dataset/{}'.format(self.dataset_name + '/smooth'),
                                                     self.img_size, self.batch_size)
        self.dataset_num = max(self.real_image_generator.num_images, self.anime_image_generator.num_images)

        self.p_model = local_variables_init()

        print()
        print("##### Information #####")
        print("# gan type : ", self.gan_type)
        print("# dataset : ", self.dataset_name)
        print("# max dataset number : ", self.dataset_num)
        print("# batch_size : ", self.batch_size)
        print("# epoch : ", self.epoch)
        print("# init_epoch : ", self.init_epoch)
        print("# training image size [H, W] : ", self.img_size)
        print("# g_adv_weight,d_adv_weight,con_weight,sty_weight,color_weight,tv_weight : ", self.g_adv_weight,
              self.d_adv_weight, self.con_weight, self.sty_weight, self.color_weight, self.tv_weight)
        print("# init_lr,g_lr,d_lr : ", self.init_lr, self.g_lr, self.d_lr)
        print(f"# training_rate G -- D: {self.training_rate} : 1")
        print()

    ##################################################################################
    # Generator
    ##################################################################################

    def generator(self):
        G = Generator()
        return G

    ##################################################################################
    # Discriminator
    ##################################################################################

    def discriminator(self):
        D = Discriminator(self.ch, self.n_dis, self.sn)
        return D

    ##################################################################################
    # Model
    ##################################################################################
    @tf.function
    def gradient_panalty(self, real, fake):
        if self.gan_type.__contains__('dragan'):
            eps = tf.random.uniform(shape=tf.shape(real), minval=0., maxval=1.)
            _, x_var = tf.nn.moments(real, axes=[0, 1, 2, 3])
            x_std = tf.sqrt(x_var)  # magnitude of noise decides the size of local region

            fake = real + 0.5 * x_std * eps

        alpha = tf.random.uniform(shape=[self.batch_size, 1, 1, 1], minval=0., maxval=1.)
        interpolated = real + alpha * (fake - real)

        disc = self.discriminator()
        disc.build(input_shape=[None, self.img_size[0], self.img_size[1], self.img_ch])

        with tf.GradientTape() as tape:
            tape.watch(interpolated)
            logit, _ = disc(interpolated)
        # gradient of D(interpolated)
        grad = tape.gradients(logit, interpolated)[0]
        grad_norm = tf.norm(tf.keras.Flatten(grad), axis=1)  # l2 norm

        GP = 0
        # WGAN - LP
        if self.gan_type.__contains__('lp'):
            GP = self.ld * tf.reduce_mean(tf.square(tf.maximum(0.0, grad_norm - 1.)))

        elif self.gan_type.__contains__('gp') or self.gan_type == 'dragan':
            GP = self.ld * tf.reduce_mean(tf.square(grad_norm - 1.))

        return GP

    def train(self):

        """ Input Image"""
        real_img_op, anime_img_op, anime_smooth_op = self.real_image_generator.load_images(), \
                                                     self.anime_image_generator.load_images(), \
                                                     self.anime_smooth_generator.load_images()

        # real, anime, anime_gray, anime_smooth = real_img_op, anime_img_op, \
        #                                         anime_img_op, anime_smooth_op
        """ Define Generator, Discriminator """
        generated = self.generator()
        discriminator = self.discriminator()

        # summary writer
        self.writer = tf.summary.create_file_writer(self.log_dir + '/' + self.model_dir)

        """ Training """

        init_optim = Adam(self.init_lr, beta_1=0.5, beta_2=0.999)
        G_optim = Adam(self.g_lr, beta_1=0.5, beta_2=0.999)
        D_optim = Adam(self.d_lr, beta_1=0.5, beta_2=0.999)

        # saver to save model
        self.saver = tf.train.Checkpoint(generated=generated, discriminator=discriminator, G_optim=G_optim,
                                         D_optim=D_optim)

        # restore check-point if it exits
        could_load, checkpoint_counter = self.load(self.checkpoint_dir)
        if could_load:
            start_epoch = checkpoint_counter + 1

            print(" [*] Load SUCCESS")
        else:
            start_epoch = 0

            print(" [!] Load failed...")

        init_mean_loss = []
        mean_loss = []
        j = self.training_rate
        for epoch in range(start_epoch, self.epoch):
            total_step = int(self.dataset_num / self.batch_size)
            with tqdm(range(total_step)) as tbar:
                for step in range(total_step):
                    real = next(real_img_op)[0]
                    anime = next(anime_img_op)[0]
                    anime_gray = next(anime_img_op)[1]
                    anime_smooth = next(anime_smooth_op)[0]

                    if epoch < self.init_epoch:
                        init_loss = self.init_train_step(generated, init_optim, epoch, real)
                        init_mean_loss.append(init_loss)
                        tbar.set_description('Epoch %d' % epoch)
                        tbar.set_postfix(init_v_loss=init_loss.numpy(), mean_v_loss=np.mean(init_mean_loss))
                        tbar.update()
                        if (step + 1) % 200 == 0:
                            init_mean_loss.clear()
                    else:
                        if j == self.training_rate:
                            # Update D network
                            d_loss = self.d_train_step(real, anime, anime_gray, anime_smooth,
                                                       generated, discriminator, D_optim, epoch)

                        # Update G network
                        g_loss = self.g_train_step(real, anime_gray, generated, discriminator, G_optim, epoch)

                        mean_loss.append([d_loss, g_loss])
                        tbar.set_description('Epoch %d' % epoch)
                        if j == self.training_rate:
                            tbar.set_postfix(d_loss=d_loss.numpy(), g_loss=g_loss.numpy(),
                                             mean_d_loss=np.mean(mean_loss, axis=0)[0],
                                             mean_g_loss=np.mean(mean_loss, axis=0)[1])
                        else:
                            tbar.set_postfix(g_loss=g_loss.numpy(), mean_g_loss=np.mean(mean_loss, axis=0)[1])
                        tbar.update()

                        if (step + 1) % 200 == 0:
                            mean_loss.clear()

                        j = j - 1
                        if j < 1:
                            j = self.training_rate

            if (epoch + 1) >= self.init_epoch and np.mod(epoch + 1, self.save_freq) == 0:
                self.save(self.checkpoint_dir, epoch)

            if epoch >= self.init_epoch - 1:
                """ Result Image """
                val_files = glob('./dataset/{}/*.*'.format('val'))
                save_path = './{}/{:03d}/'.format(self.sample_dir, epoch)
                check_folder(save_path)
                for i, sample_file in enumerate(val_files):
                    print('val: ' + str(i) + sample_file)
                    sample_image = np.asarray(load_test_data(sample_file, self.img_size))
                    test_real = sample_image
                    test_generated_predict = generated.predict(test_real)
                    save_images(test_real, save_path + '{:03d}_a.jpg'.format(i), None)
                    save_images(test_generated_predict, save_path + '{:03d}_b.jpg'.format(i), None)

                save_model_path = 'save_model'
                if not os.path.exists(save_model_path):
                    os.makedirs(save_model_path)
                tf.saved_model.save(generated, os.path.join(save_model_path, 'generated'))

    @tf.function
    def init_train_step(self, generated, init_optim, epoch, real):
        with tf.GradientTape() as tape:
            generator_images = generated(real)
            # init pharse
            init_c_loss = con_loss(self.p_model, real, generator_images)
            init_loss = self.con_weight * init_c_loss
        grads = tape.gradient(init_loss, generated.trainable_variables)
        init_optim.apply_gradients(zip(grads, generated.trainable_variables))
        with self.writer.as_default(step=epoch):
            """" Summary """
            tf.summary.scalar(name='G_init', data=init_loss)
        return init_loss

    @tf.function
    def g_train_step(self, real, anime_gray, generated,
                     discriminator, G_optim, epoch):
        with tf.GradientTape() as tape:
            fake_image = generated(real)
            generated_logit = discriminator(fake_image)
            # gan
            c_loss, s_loss = con_sty_loss(self.p_model, real, anime_gray, fake_image)
            tv_loss = self.tv_weight * total_variation_loss(fake_image)
            t_loss = self.con_weight * c_loss + self.sty_weight * s_loss + color_loss(real,
                                                                                      fake_image) * self.color_weight + tv_loss

            g_loss = self.g_adv_weight * generator_loss(self.gan_type, generated_logit)
            Generator_loss = t_loss + g_loss

        grads = tape.gradient(Generator_loss, generated.trainable_variables)
        G_optim.apply_gradients(zip(grads, generated.trainable_variables))
        with self.writer.as_default(step=epoch):
            """" Summary """
            self.G_loss = tf.summary.scalar("Generator_loss", Generator_loss)

            self.G_gan = tf.summary.scalar("G_gan", g_loss)
            self.G_vgg = tf.summary.scalar("G_pre_model", t_loss)
        return Generator_loss

    @tf.function
    def d_train_step(self, real, anime, anime_gray, anime_smooth, generated, discriminator,
                     D_optim, epoch):

        with tf.GradientTape() as tape:
            fake_image = generated(real)
            d_anime_logit = discriminator(anime)
            d_anime_gray_logit = discriminator(anime_gray)
            d_smooth_logit = discriminator(anime_smooth)
            generated_logit = discriminator(fake_image)
            """ Define Loss """
            if self.gan_type.__contains__('gp') or self.gan_type.__contains__('lp') or \
                    self.gan_type.__contains__('dragan'):
                GP = self.gradient_panalty(real=anime, fake=fake_image)
            else:
                GP = 0.0

            d_loss = self.d_adv_weight * discriminator_loss(self.gan_type, d_anime_logit, d_anime_gray_logit,
                                                            generated_logit,
                                                            d_smooth_logit) + GP
        grads = tape.gradient(d_loss, discriminator.trainable_variables)
        D_optim.apply_gradients(zip(grads, discriminator.trainable_variables))

        with self.writer.as_default(step=epoch):
            """" Summary """
            self.D_loss = tf.summary.scalar("Discriminator_loss", d_loss)

        return d_loss

    @property
    def model_dir(self):
        return "{}_{}_{}_{}_{}_{}_{}_{}_{}".format(self.model_name, self.dataset_name,
                                                   self.gan_type,
                                                   int(self.g_adv_weight), int(self.d_adv_weight),
                                                   int(self.con_weight), int(self.sty_weight),
                                                   int(self.color_weight), int(self.tv_weight))

    def save(self, checkpoint_dir, epoch):
        checkpoint_dir = os.path.join(checkpoint_dir, self.model_dir)
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        ckpt_manager = tf.train.CheckpointManager(self.saver, checkpoint_dir,
                                                  max_to_keep=5)
        ckpt_manager.save(checkpoint_number=epoch)

    def load(self, checkpoint_dir):
        print(" [*] Reading checkpoints...")
        checkpoint_dir = os.path.join(checkpoint_dir, self.model_dir)

        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)  # checkpoint file information

        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)  # first line
            self.saver.restore(os.path.join(checkpoint_dir, ckpt_name))
            counter = int(ckpt_name.split('-')[-1])
            print(" [*] Success to read {}".format(os.path.join(checkpoint_dir, ckpt_name)))
            return True, counter
        else:
            print(" [*] Failed to find a checkpoint")
            return False, 0