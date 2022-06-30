from ray.rllib.utils.framework import try_import_torch
from ray.rllib.evaluation.postprocessing import adjust_nstep
from ray.rllib.policy.policy import Policy
from ray.rllib.policy.sample_batch import SampleBatch
import numpy as np
from ray.rllib.utils.torch_ops import convert_to_torch_tensor
from ray.rllib.utils.numpy import convert_to_numpy

torch, nn = try_import_torch()

"""
centralized Q postprocessing for 
1. MADDPG 
"""

def get_dim(a):
    dim = 1
    for i in a:
        dim *= i
    return dim


class CentralizedQValueMixin:

    def __init__(self):
        self.compute_central_q = self.model.get_cc_q_values


def centralized_critic_q(policy: Policy,
                         sample_batch: SampleBatch,
                         other_agent_batches=None,
                         episode=None) -> SampleBatch:
    custom_config = policy.config["model"]["custom_model_config"]
    pytorch = custom_config["framework"] == "torch"
    obs_dim = get_dim(custom_config["space_obs"]["obs"].shape)
    algorithm = custom_config["algorithm"]
    opp_action_in_cc = custom_config["opp_action_in_cc"]
    global_state_flag = custom_config["global_state_flag"]
    mask_flag = custom_config["mask_flag"]

    if mask_flag:
        action_mask_dim = custom_config["space_act"].n
    else:
        action_mask_dim = 0

    n_agents = custom_config["num_agents"]
    opponent_agents_num = n_agents - 1

    if (pytorch and hasattr(policy, "compute_central_q")) or \
            (not pytorch and policy.loss_initialized()):

        if not opp_action_in_cc and global_state_flag:
            sample_batch["state"] = sample_batch['obs'][:, action_mask_dim + obs_dim:]
            sample_batch["new_state"] = sample_batch['new_obs'][:, action_mask_dim + obs_dim:]
            raise ValueError("offpolicy centralized critic without action is illegal")

        else:  # need opponent info
            assert other_agent_batches is not None
            opponent_batch_list = list(other_agent_batches.values())
            raw_opponent_batch = [opponent_batch_list[i][1] for i in range(opponent_agents_num)]
            opponent_batch = []
            for one_opponent_batch in raw_opponent_batch:
                if len(one_opponent_batch) == len(sample_batch):
                    pass
                else:
                    if len(one_opponent_batch) > len(sample_batch):
                        one_opponent_batch = one_opponent_batch.slice(0, len(sample_batch))
                    else:  # len(one_opponent_batch) < len(sample_batch):
                        length_dif = len(sample_batch) - len(one_opponent_batch)
                        one_opponent_batch = one_opponent_batch.concat(
                            one_opponent_batch.slice(len(one_opponent_batch) - length_dif, len(one_opponent_batch)))
                opponent_batch.append(one_opponent_batch)

            # all other agent obs as state
            # sample_batch["state"] = sample_batch['obs'][:, action_mask_dim:action_mask_dim + obs_dim]
            if global_state_flag:
                sample_batch["state"] = sample_batch['obs'][:, action_mask_dim + obs_dim:]
                sample_batch["new_state"] = sample_batch['new_obs'][:, action_mask_dim + obs_dim:]

            else:
                sample_batch["state"] = np.stack(
                    [sample_batch['obs'][:, action_mask_dim:action_mask_dim + obs_dim]] + [
                        opponent_batch[i]["obs"][:, action_mask_dim:action_mask_dim + obs_dim] for i in
                        range(opponent_agents_num)], 1)
                sample_batch["new_state"] = np.stack(
                    [sample_batch['new_obs'][:, action_mask_dim:action_mask_dim + obs_dim]] + [
                        opponent_batch[i]["new_obs"][:, action_mask_dim:action_mask_dim + obs_dim] for i in
                        range(opponent_agents_num)], 1)

            sample_batch["opponent_actions"] = np.stack(
                [opponent_batch[i]["actions"] for i in range(opponent_agents_num)],
                1)
            sample_batch["prev_opponent_actions"] = np.stack(
                [opponent_batch[i]["prev_actions"] for i in range(opponent_agents_num)],
                1)


    else:
        # Policy hasn't been initialized yet, use zeros.
        o = sample_batch[SampleBatch.CUR_OBS]
        if global_state_flag:
            sample_batch["state"] = np.zeros((o.shape[0], get_dim(custom_config["space_obs"]["state"].shape)),
                                             dtype=sample_batch[SampleBatch.CUR_OBS].dtype)
            sample_batch["new_state"] = np.zeros((o.shape[0], get_dim(custom_config["space_obs"]["state"].shape)),
                                                 dtype=sample_batch[SampleBatch.CUR_OBS].dtype)
        else:
            sample_batch["state"] = np.zeros((o.shape[0], n_agents, obs_dim),
                                             dtype=sample_batch[SampleBatch.CUR_OBS].dtype)
            sample_batch["new_state"] = np.zeros((o.shape[0], n_agents, obs_dim),
                                                 dtype=sample_batch[SampleBatch.CUR_OBS].dtype)

        sample_batch["vf_preds"] = np.zeros_like(
            sample_batch[SampleBatch.REWARDS], dtype=np.float32)
        sample_batch["opponent_actions"] = np.stack(
            [np.zeros_like(sample_batch["actions"], dtype=sample_batch["actions"].dtype) for _ in
             range(opponent_agents_num)], axis=1)
        sample_batch["prev_opponent_actions"] = np.stack(
            [np.zeros_like(sample_batch["actions"], dtype=sample_batch["actions"].dtype) for _ in
             range(opponent_agents_num)], axis=1)
        sample_batch["next_opponent_actions"] = np.stack(
            [np.zeros_like(sample_batch["actions"], dtype=sample_batch["actions"].dtype) for _ in
             range(opponent_agents_num)], axis=1)

    # N-step reward adjustments.
    if policy.config["n_step"] > 1:
        adjust_nstep(policy.config["n_step"], policy.config["gamma"], sample_batch)

    # Create dummy prio-weights (1.0) in case we don't have any in
    # the batch.
    if "weights" not in sample_batch:
        sample_batch["weights"] = np.ones_like(sample_batch[SampleBatch.REWARDS])

    # Prioritize on the worker side.
    if sample_batch.count > 0 and policy.config["worker_side_prioritization"]:
        td_errors = policy.compute_td_error(
            sample_batch[SampleBatch.OBS], sample_batch[SampleBatch.ACTIONS],
            sample_batch[SampleBatch.REWARDS], sample_batch[SampleBatch.NEXT_OBS],
            sample_batch[SampleBatch.DONES], sample_batch["weights"])
        new_priorities = (np.abs(convert_to_numpy(td_errors)) +
                          policy.config["prioritized_replay_eps"])
        sample_batch["weights"] = new_priorities

    return sample_batch


# postprocessing sampled batch before learning stage.
def before_learn_on_batch(multi_agent_batch, policies, train_batch_size):
    other_agent_next_action_dict = {}
    all_agent_next_action = []
    for pid, policy in policies.items():

        # get agent number:
        if 0 not in other_agent_next_action_dict:
            custom_config = policy.config["model"]["custom_model_config"]
            n_agents = custom_config["num_agents"]
            for i in range(n_agents):
                other_agent_next_action_dict[i] = []

        policy_batch = multi_agent_batch.policy_batches[pid]
        target_policy_model = policy.target_model.policy_model.to(policy.device)
        next_obs = policy_batch["new_obs"]

        input_dict = {"obs": {}}
        input_dict["obs"]["obs"] = next_obs

        state_in = policy_batch["state_in_0"]
        seq_lens = policy_batch["seq_lens"]

        input_dict = convert_to_torch_tensor(input_dict, policy.device)
        state_in = convert_to_torch_tensor(state_in, policy.device).unsqueeze(0)
        seq_lens = convert_to_torch_tensor(seq_lens, policy.device)

        next_action_out, _ = target_policy_model.forward(input_dict, state_in, seq_lens)
        next_action = target_policy_model.action_out_squashed(next_action_out)
        next_action = convert_to_numpy(next_action)

        agent_id = np.unique(policy_batch["agent_index"])
        for a_id in agent_id:
            valid_flag = np.where(policy_batch["agent_index"] == a_id)[0]
            next_action_one_agent = next_action[valid_flag, :]
            all_agent_next_action.append(next_action_one_agent)
            for key in other_agent_next_action_dict.keys():
                if key != a_id:
                    other_agent_next_action_dict[a_id].append(next_action_one_agent)

    # construct opponent next action for each batch
    all_agent_next_action = np.stack(all_agent_next_action, 1)
    for pid, policy in policies.items():
        policy_batch = multi_agent_batch.policy_batches[pid]
        agent_id = np.unique(policy_batch["agent_index"])
        agent_num = len(agent_id)
        ls = []
        for a in range(agent_num):
            ls.append(all_agent_next_action)
        next_action_batch = np.stack(ls, 1).reshape((policy_batch.count, n_agents, -1))
        other_next_action_batch_ls = []
        for i in range(policy_batch.count):
            current_agent_id = policy_batch["agent_index"][i]
            next_action_ts = next_action_batch[i]
            other_next_action_ts = np.delete(next_action_ts, current_agent_id, axis=0)
            other_next_action_batch_ls.append(other_next_action_ts)
        other_next_action_batch = np.stack(other_next_action_batch_ls, 0)
        multi_agent_batch.policy_batches[pid]["next_opponent_actions"] = other_next_action_batch

    return multi_agent_batch
