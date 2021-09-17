import random
import sys
import time

import torch
import threading
import torch.backends.cudnn
import json
import torch.multiprocessing as mp
from agent.algorithms.v_trace import v_trace
from agent.manager.native_worker_manager import NativeWorkerManager
from rollout_storage.elite_set.buf_population_strategy.brute_force_strategy import BruteForceStrategy
from rollout_storage.elite_set.buf_population_strategy.lim_inf_strategy import LimInfStrategy
from rollout_storage.elite_set.buf_population_strategy.lim_zero_strategy import LimZeroStrategy
from rollout_storage.elite_set.elite_set_replay import EliteSetReplay
from rollout_storage.experience_replay_proxy import ExperienceReplayProxy
from rollout_storage.experience_replay import ExperienceReplayTorch
from rollout_storage.writer_queue.alternating_strategy import AlternatingStrategy
from rollout_storage.writer_queue.keep_latest_strategy import KeepLatestStrategy
from rollout_storage.writer_queue.keep_oldest_strategy import KeepOldestStrategy

from rollout_storage.writer_queue.replay_buffer_writer import ReplayWriterQueue
from torch.optim import Adam
from model.network import ModelNetwork
from scheduler.multi_step_lr import MultiStepLRStr
from stats.stats import Statistics
from utils import logger
from threading import Condition


class Learner(object):
    def __init__(self, flags, run_id):
        self.run_id = run_id
        self.flags = flags
        self.file_save_dir_url = "results/" + self.flags.env + "_" + str(self.run_id)

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if torch.cuda.is_available():
            logger.info("Learner is using CUDA - GPU execution")
        else:
            logger.info("CUDA not available - CPU execution")
        self.model = ModelNetwork(self.flags.actions_count).to(self.device)
        self.feature_reset_model = ModelNetwork(self.flags.actions_count).eval()

        if self.flags.op_mode == "train_w_load":
            self.model.load_state_dict(torch.load(self.flags.load_model_uri)["model_state_dict"])
            logger.info("Model state successfully loaded from file save")

        self.optimizer = Adam(self.model.parameters(), lr=self.flags.lr)
        if not hasattr(self.optimizer, '__str__'):
            raise MissingMethod("Optimizer doesnt have __str__ method implemented, which is required")

        # self.lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.optimizer, 0.9999)
        self.lr_scheduler = MultiStepLRStr(self.optimizer, [(i * 40) + 40 for i in range(100)], gamma=0.97)
        if not hasattr(self.lr_scheduler, '__str__'):
            raise MissingMethod("Scheduler doesnt have __str__ method implemented, which is required")

        self.learning_lock = threading.Lock()
        self.batch_lock = threading.Lock()
        self.mini_batcher_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.training_event = threading.Event()

        self.stats = Statistics(self.stop_event, self.file_save_dir_url, self.flags, str(self.optimizer), str(self.lr_scheduler), True, self.flags.background_save)

        self.training_iteration = 0

        self.replay_buffers = []

        if self.flags.use_replay_buffer:
            self.replay_buffers.append(ExperienceReplayProxy(ExperienceReplayTorch(self.batch_lock, self.flags), self.file_save_dir_url))

        if self.flags.use_elite_set:
            elite_pop_strategy = None
            if self.flags.elite_pop_strategy == "lim_zero":
                elite_pop_strategy = LimZeroStrategy(self.flags)
            elif self.flags.elite_pop_strategy == "lim_inf":
                elite_pop_strategy = LimInfStrategy(self.flags)
            elif self.flags.elite_pop_strategy == "brute_force":
                elite_pop_strategy = BruteForceStrategy(self.flags)
            self.replay_buffers.append(ExperienceReplayProxy(EliteSetReplay(self.batch_lock, (self.model.get_flatten_layer_output_size(),), elite_pop_strategy, self.flags, self.file_save_dir_url), self.file_save_dir_url))

        if len(self.replay_buffers) == 0:
            raise ForbiddenSetting(
                "No replay buffer has been selected - application has not been modified to work without any buffer to store worker trajectories")

        replay_writer_strategy = None
        if self.flags.discarding_strategy == "keep_latest":
            replay_writer_strategy = KeepLatestStrategy()
        elif self.flags.discarding_strategy == "keep_oldest":
            replay_writer_strategy = KeepOldestStrategy()
        elif self.flags.discarding_strategy == "alternating":
            replay_writer_strategy = AlternatingStrategy()
        self.replay_writer = ReplayWriterQueue(self.replay_buffers, queue_size=self.flags.replay_writer_queue_size,
                                               fill_in_strategy=replay_writer_strategy, flags=self.flags)
        if not self.flags.reproducible:
            self.replay_writer.start()

        if self.flags.multiprocessing_backend == "ray":
            from agent.manager.ray_worker_manager import RayWorkerManager # local import so ray is not imported when using python_native multiprocessing
            self.worker_manager = RayWorkerManager(self.stop_event, self.training_event, self.replay_writer, self.replay_buffers, self.model, self.stats, self.flags, self.file_save_dir_url, False)
        elif self.flags.multiprocessing_backend == "python_native":
            if mp.get_start_method() != "spawn":
                self.replay_writer.close()
                self.stats.close()
                raise ForbiddenSetting("This app only supports 'spawn' type sub-processes when working with python-native backend")
            self.worker_manager = NativeWorkerManager(self.stop_event, self.training_event, self.replay_writer, self.replay_buffers, self.model, self.stats, self.flags, self.file_save_dir_url, False)

        self.batch_size = self.flags.batch_size
        self.data_pos_pointer = 0
        self.in_row_condition = Condition()
        self.condition_waiters = 0

    def start(self):
        threads = []
        thread = threading.Thread(target=self.worker_manager.manage_workers, name="manage_workers")
        thread.start()
        threads.append(thread)

        for i in range(self.flags.learner_thread_count):
            thread = threading.Thread(target=self.learning, name="learning_thread-%d" % i)
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        logger.info("Training has ended. Beginning to clean up resources and process collected data.")
        self._save_current_model_state()
        self.replay_writer.close()
        self.worker_manager.reset()
        self.stats.close()
        return 0

    def learning(self):
        local_random = random.Random(self.flags.seed)
        try:
            for p in range(len(self.replay_buffers)):
                self.replay_buffers[p].replay_filled_event.wait()

            self.training_event.set()
            self.stats.mark_warm_up_period()
            while self.training_iteration < self.flags.training_max_steps:
                if self.stop_event.is_set():
                    break
                self._learning_iteration(local_random)
        except Exception as exp:
            logger.exception("Learning thread raise new exception - ending execution")
        self.worker_manager.update_model_data(self.model)
        self.stop_event.set()

    def _learning_iteration(self, local_random):
        try:
            actions, beh_logits, not_done, rewards, states, counter = self._prepare_batch(local_random)
            if actions is None:
                return

            if states.shape[1] != self.batch_size:
                return

            if self.flags.reproducible:
                def is_ready_for_processing():
                    nonlocal counter
                    if counter < self.data_pos_pointer:
                        raise ValueError("COunter cannot be lower that global counter")
                    return counter == self.data_pos_pointer

                if self.condition_waiters == (self.flags.learner_thread_count - 1):
                    with self.in_row_condition:
                        self.in_row_condition.notify(1)

                if counter > self.data_pos_pointer:
                    with self.in_row_condition:
                        self.condition_waiters += 1
                        self.in_row_condition.wait_for(is_ready_for_processing)
                        self.condition_waiters -= 1

            with self.learning_lock:
                self.training_iteration += 1

                if self.flags.reproducible:
                    self.data_pos_pointer = (self.data_pos_pointer + 1) % self.flags.max_cache_pos_pointer

                    if self.condition_waiters > 0:
                        with self.in_row_condition:
                            self.in_row_condition.notify(1)

                bootstrap_value, current_logits, current_values = self._foward_pass(states)

                baseline_loss, entropy_loss, policy_loss = v_trace(actions, beh_logits, bootstrap_value,
                                                                   current_logits, current_values, not_done, rewards, self.device, self.flags, self.batch_size)

                self._backprop(policy_loss, baseline_loss, entropy_loss)
                if self.flags.reproducible and self.training_iteration % self.flags.replay_out_cache_size == 0:
                    self.worker_manager.update_model_data(self.model)
        except RuntimeError as exp:
            if 'out of memory' in str(exp):
                logger.warning("System ran out of memory, trying to lower the batch_size")
                for p in self.model.parameters():
                    if p.grad is not None:
                        del p.grad
                torch.cuda.empty_cache()
                time.sleep(5)
                self.batch_size -= 2
                if self.batch_size <= 0:
                    logger.exception("Unable to lower batch_size anymore - cannot handle memory overflow")
                    raise exp
                if self.flags.reproducible:
                    logger.exception("Dynamic batch_size is NOT allowed in SYNCHRONIZED - REPLICABLE mode")
                    raise exp
                self.stats.change_batch_size(self.batch_size)
                return
            else:
                raise exp

        if not self.flags.reproducible:
            self.worker_manager.update_model_data(self.model)
        if self.flags.use_elite_set:
            self._update_elite_features()
        if self.training_iteration % self.flags.save_model_period == 0:
            self._save_current_model_state()

    def _foward_pass(self, states):
        current_logits, current_values = self.model(states.detach(), no_feature_vec=True)

        bootstrap_value = current_values[-1].squeeze(-1)
        current_values = current_values.squeeze(-1)

        return bootstrap_value, current_logits, current_values

    def _backprop(self, policy_loss, baseline_loss, entropy_loss):
        loss = policy_loss + baseline_loss + entropy_loss
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.flags.max_grad_norm)
        self.optimizer.step()
        self.lr_scheduler.step()
        self.stats.process_learning_iter(policy_loss.item(), baseline_loss.item(), entropy_loss.item(), self.lr_scheduler.get_last_lr()[0])

    def _prepare_batch(self, local_random):
        if self.flags.reproducible:
            self.mini_batcher_lock.acquire()
        states, actions, rewards, beh_logits, not_done, counter = self.replay_buffers[0].random_sample(
            self.batch_size, local_random)
        if states is None:
            return actions, beh_logits, not_done, rewards, states, counter

        for i in range(1, len(self.replay_buffers)):
            states_n, actions_n, rewards_n, beh_logits_n, not_done_n, ctr = self.replay_buffers[i].random_sample(
                self.batch_size, local_random)
            if self.flags.reproducible and counter != ctr:
                raise ThreadingSyncError("Data indexes of mini-batches should be the same ! - synchronization error")
            if states_n is None:
                return actions_n, beh_logits_n, not_done_n, rewards_n, states_n
            states = torch.cat((states, states_n), 0)
            actions = torch.cat((actions, actions_n), 0)
            rewards = torch.cat((rewards, rewards_n), 0)
            beh_logits = torch.cat((beh_logits, beh_logits_n), 0)
            not_done = torch.cat((not_done, not_done_n), 0)
        if self.flags.reproducible:
            self.mini_batcher_lock.release()

        states, actions, rewards, beh_logits, not_done = states.to(self.device).transpose(1, 0), actions.to(
            self.device).transpose(1, 0), rewards.to(self.device).transpose(1, 0), beh_logits.to(self.device).transpose(
            1, 0), not_done.to(self.device).transpose(1, 0)

        return actions, beh_logits, not_done, rewards, states, counter

    def _update_elite_features(self):
        if self.training_iteration % self.flags.elite_reset_period == 0:
            with self.batch_lock:
                logger.info("Updating elite set feature_vecs with current model policy")
                for i in range(len(self.replay_buffers)):
                    if (isinstance(self.replay_buffers[i], ExperienceReplayProxy) and isinstance(self.replay_buffers[i].experience_replay, EliteSetReplay)) or isinstance(self.replay_buffers[i], EliteSetReplay):

                        prior_states = self.replay_buffers[i].get_prior_buf_states()

                        self.feature_reset_model.load_state_dict({k: v.cpu() for k, v in self.model.state_dict().items()})

                        with torch.no_grad():
                            _, _, feature_vecs_prior = self.feature_reset_model(prior_states, True)
                        self.replay_buffers[i].set_feature_vecs_prior(feature_vecs_prior)

                        # feature_vecoefs = None
                        # for j in range(6):
                        #     with torch.no_grad():
                        #         _, _, feature_vecoefs_prior = self.model(prior_states[j*100:(j+1)*100].cuda(), True)
                        #     if feature_vecoefs is None:
                        #         feature_vecoefs = feature_vecoefs_prior.cpu()
                        #     else:
                        #         feature_vecoefs = torch.cat((feature_vecoefs, feature_vecoefs_prior.cpu()), 0)
                        # self.replay_buffer.set_feature_vecoefs_prior(feature_vecoefs)

    def _save_current_model_state(self):
        torch.save({"model_state_dict": self.model.state_dict(),
                    "optimizer_state_dict": self.optimizer.state_dict(),
                    "scheduler_state_dict": self.lr_scheduler.state_dict(),
                    "flags": vars(self.flags),
                    },
                   self.file_save_dir_url + '/regular_model_save_.pt')
        with open(self.file_save_dir_url + '/options_flags.json', 'w') as file:
            json.dump(self.flags.__dict__, file, indent=2)

    def _load_model_state(self, url):
        state_dict = torch.load(url)
        self.model.load_state_dict(state_dict["model_state_dict"])
        self.optimizer.load_state_dict(state_dict["optimizer_state_dict"])
        self.lr_scheduler.load_state_dict(state_dict["scheduler_state_dict"])
        self.flags = state_dict["flags"]


class MissingMethod(Exception):
    pass


class ForbiddenSetting(Exception):
    pass


class ThreadingSyncError(Exception):
    pass

