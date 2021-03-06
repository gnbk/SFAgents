from MAMEToolkit.sf_environment import Environment
from tensorflow_complex3.model.ModeNetwork import ModeNetwork
from tensorflow_complex3.model.MoveNetwork import MoveNetwork
from tensorflow_complex3.model.AttackNetwork import AttackNetwork
from tensorflow_complex3.model.MoveAttackNetwork import MoveAttackNetwork
import tensorflow_complex3.WorkerUtils as wu
import tensorflow as tf
import numpy as np
import traceback
import logging
from pathlib import Path
import gc


def update_target_graph(from_scope, to_scope):
    from_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, from_scope)
    to_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, to_scope)

    op_holder = []
    for from_var, to_var in zip(from_vars, to_vars):
        op_holder.append(to_var.assign(from_var))
    return op_holder


def eval(worker_no, roms_path, config, learning_rate, frames_per_step, stats, saves_path):
    name = "worker%d" % worker_no

    mode_network = ModeNetwork("global", learning_rate, frames_per_step=frames_per_step)
    move_network = MoveNetwork("global", learning_rate, frames_per_step=frames_per_step)
    attack_network = AttackNetwork("global", learning_rate, frames_per_step=frames_per_step)
    move_attack_network = MoveAttackNetwork("global", learning_rate, frames_per_step=frames_per_step)

    saver = tf.train.Saver(max_to_keep=1, var_list=tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='global'))

    env = Environment(name, roms_path, difficulty=3, frames_per_step=1, frame_ratio=3)
    frames = [env.start()]
    for _ in range(frames_per_step - 1):
        frames.append(env.step(8, 9)[0])

    with tf.Session(config=config) as sess:
        saver.restore(sess, f'{saves_path}/{stats.get_episode()}/model.ckpt')
        while True:
            game_done = False
            total_reward = 0

            while not game_done:
                observation = wu.prepro(frames, frames_per_step)

                mode_out = sess.run(mode_network.mode_out_sym, feed_dict={mode_network.observation_sym: observation})
                mode_hot = wu.choose_action(mode_out)
                mode = np.argmax(mode_hot)

                if mode == 0:
                    move_out = sess.run(move_network.move_out_sym, feed_dict={move_network.observation_sym: observation})
                    move_hot = wu.choose_action(move_out)
                    move_action = np.argmax(move_hot)
                    attack_action = 9
                elif mode == 1:
                    attack_out = sess.run(attack_network.attack_out_sym, feed_dict={attack_network.observation_sym: observation})
                    attack_hot = wu.choose_action(attack_out)
                    move_action = 8
                    attack_action = np.argmax(attack_hot)
                elif mode == 2:
                    move_out, attack_out = sess.run([move_attack_network.move_out_sym, move_attack_network.attack_out_sym], feed_dict={move_attack_network.observation_sym: observation})
                    move_hot = wu.choose_action(move_out)
                    attack_hot = wu.choose_action(attack_out)
                    move_action = np.argmax(move_hot)
                    attack_action = np.argmax(attack_hot)
                else:
                    raise EnvironmentError(f"Generated invalid mode '{mode}'")

                _, _, round_done, stage_done, game_done = env.step(move_action, attack_action)

                if round_done:
                    if game_done:
                        stats.update({"score": total_reward, "stage": env.stage})
                    frames = [env.reset()]
                    for _ in range(frames_per_step - 1):
                        frames.append(env.step(8, 9)[0])


def run(worker_no, roms_path, learning_rate, frames_per_step, cluster, config, data_bins, stats, save_frequency, saves_path):
    name = "worker%d" % worker_no

    with tf.device(tf.train.replica_device_setter(worker_device="/job:worker/task:%d" % worker_no, cluster=cluster)):
        ModeNetwork("global", learning_rate, frames_per_step=frames_per_step)
        MoveNetwork("global", learning_rate, frames_per_step=frames_per_step)
        AttackNetwork("global", learning_rate, frames_per_step=frames_per_step)
        MoveAttackNetwork("global", learning_rate, frames_per_step=frames_per_step)

    # saver = tf.train.import_meta_graph(f'{saves_path}/{stats.get_episode()}', clear_devices=True)

    saver = tf.train.Saver(max_to_keep=1, var_list=tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='global'))

    mode_network = ModeNetwork(name, learning_rate, frames_per_step=frames_per_step)
    move_network = MoveNetwork(name, learning_rate, frames_per_step=frames_per_step)
    attack_network = AttackNetwork(name, learning_rate, frames_per_step=frames_per_step)
    move_attack_network = MoveAttackNetwork(name, learning_rate, frames_per_step=frames_per_step)

    update_local_ops = update_target_graph('global', name)

    server = tf.train.Server(cluster, job_name="worker", task_index=worker_no, config=config)
    with tf.train.MonitoredTrainingSession(master=server.target) as sess:
        try:
            if stats.get_episode() != 0:
                saver.restore(sess._sess._sess._sess._sess, f'{saves_path}/{stats.get_episode()}/model.ckpt')

            sess.run(update_local_ops)
            print("Started Worker Updates")

            env = Environment(name, roms_path, difficulty=3, frames_per_step=1, frame_ratio=3, render=True)
            frames = [env.start()]
            for _ in range(frames_per_step - 1):
                frames.append(env.step(8, 9)[0])

            while True:
                rewards = [{"reward": [], "mode": []}]
                game_done = False
                current_round = 0
                total_reward = 0
                gc.collect()

                while not game_done:

                    observation = wu.prepro(frames, frames_per_step)

                    mode_out = sess.run(mode_network.mode_out_sym, feed_dict={mode_network.observation_sym: observation})
                    mode_hot = wu.choose_action(mode_out)
                    mode = np.argmax(mode_hot)

                    if mode == 0:
                        move_out = sess.run(move_network.move_out_sym, feed_dict={move_network.observation_sym: observation})
                        move_hot = wu.choose_action(move_out)
                        data_bins.insert_move_bin(worker_no, observation, move_hot)
                        move_action = np.argmax(move_hot)
                        attack_action = 9
                    elif mode == 1:
                        attack_out = sess.run(attack_network.attack_out_sym, feed_dict={attack_network.observation_sym: observation})
                        attack_hot = wu.choose_action(attack_out)
                        data_bins.insert_attack_bin(worker_no, observation, attack_hot)
                        move_action = 8
                        attack_action = np.argmax(attack_hot)
                    elif mode == 2:
                        move_out, attack_out = sess.run([move_attack_network.move_out_sym, move_attack_network.attack_out_sym], feed_dict={move_attack_network.observation_sym: observation})
                        move_hot = wu.choose_action(move_out)
                        attack_hot = wu.choose_action(attack_out)
                        data_bins.insert_move_attack_bin(worker_no, observation, move_hot, attack_hot)
                        move_action = np.argmax(move_hot)
                        attack_action = np.argmax(attack_hot)
                    else:
                        raise EnvironmentError(f"Generated invalid mode '{mode}'")

                    frame, r, round_done, stage_done, game_done = env.step(move_action, attack_action)
                    frames.pop(0)
                    frames.append(frame)

                    total_reward += r["P1"]
                    rewards[current_round]["reward"].append(r["P1"])
                    rewards[current_round]["mode"].append(mode)

                    if round_done:
                        gc.collect()
                        if game_done:
                            move_rewards, attack_rewards, move_attack_rewards = wu.compile_rewards(rewards)
                            if move_rewards is not None:
                                all_observations, all_move_actions, all_modes = data_bins.empty_move_bin(worker_no)
                                mode_network.train(sess, all_observations, all_modes, move_rewards)
                                move_network.train(sess, all_observations, all_move_actions, move_rewards)
                                all_observations = None
                                all_move_actions = None
                                all_modes = None
                                gc.collect()
                            else:
                                logging.info("No move rewards found")

                            if attack_rewards is not None:
                                all_observations, all_attack_actions, all_modes = data_bins.empty_attack_bin(worker_no)
                                mode_network.train(sess, all_observations, all_modes, attack_rewards)
                                attack_network.train(sess, all_observations, all_attack_actions, attack_rewards)
                                all_observations = None
                                all_attack_actions = None
                                all_modes = None
                                gc.collect()
                            else:
                                logging.info("No attack rewards found")

                            if move_attack_rewards is not None:
                                all_observations, all_move_actions, all_attack_actions, all_modes = data_bins.empty_move_attack_bin(worker_no)
                                mode_network.train(sess, all_observations, all_modes, move_attack_rewards)
                                move_attack_network.train(sess, all_observations, all_move_actions, all_attack_actions, move_attack_rewards)
                                all_observations = None
                                all_move_actions = None
                                all_attack_actions = None
                                all_modes = None
                                gc.collect()
                            else:
                                logging.info("No move attack rewards found")

                            sess.run(update_local_ops)
                            stats.update({"score": total_reward, "stage": env.stage})
                            if stats.get_episode() > 0 and stats.get_episode() % save_frequency == 0:
                                path = Path(f'{saves_path}/{stats.get_episode()}')
                                if path.exists():
                                    path.unlink()
                                path.mkdir()
                                saver.save(sess._sess._sess._sess._sess, f'{saves_path}/{stats.get_episode()}/model.ckpt')

                        current_round += 1
                        rewards.append({"reward": [], "mode": []})
                        frames = [env.reset()]
                        for _ in range(frames_per_step - 1):
                            frames.append(env.step(8, 9)[0])
        except:
            error = traceback.format_exc()
            print(error)
            logging.error(error)
            exit(1)
