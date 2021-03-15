# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import os
from collections import defaultdict

import numpy as np
import torch
from torch import nn
from torch.distributions import Categorical
from torch.nn.utils import clip_grad

from maro.rl import AbsAgent


class GNNBasedActorCriticConfig:
    """Configuration for the GNN-based Actor Critic algorithm.
    
    Args:
        p2p_adj (numpy.array): Adjencency matrix for static nodes.
        td_steps (int): The value "n" in the n-step TD algorithm.
        gamma (float): The time decay.
        actor_loss_coefficient (float): Coefficient for actor loss in total loss.
        entropy_factor (float): The weight of the policy"s entropy to boost exploration.
    """
    __slots__ = [
        "p2p_adj", "td_steps", "reward_discount", "value_discount", "actor_loss_coefficient", "entropy_factor"
    ]

    def __init__(
        self,
        p2p_adj: np.ndarray,
        td_steps: int = 100, 
        reward_discount: float = 0.97,
        actor_loss_coefficient: float = 0.1,
        entropy_factor: float = 0.1
    ):
        self.p2p_adj = p2p_adj
        self.td_steps = td_steps
        self.reward_discount = reward_discount
        self.value_discount = reward_discount ** 100
        self.actor_loss_coefficient = actor_loss_coefficient
        self.entropy_factor = entropy_factor


class GNNBasedActorCritic(AbsAgent):
    """GNN-based Actor-Critic."""
    def choose_action(self, state: dict):
        """Get action from the AC model.

        Args:
            state (dict): A dictionary containing the input to the module. For example:
                {
                    "v": v,
                    "p": p,
                    "pe": {
                        "edge": pedge,
                        "adj": padj,
                        "mask": pmask,
                    },
                    "ve": {
                        "edge": vedge,
                        "adj": vadj,
                        "mask": vmask,
                    },
                    "ppe": {
                        "edge": ppedge,
                        "adj": p2p_adj,
                        "mask": p2p_mask,
                    },
                    "mask": seq_mask,
                }

        Returns:
            model_action (numpy.int64): The action returned from the module.
        """
        single = len(state["p"].shape) == 3
        action_prob, _ = self.model(
            self.union(state), p_idx=state["p_idx"], v_idx=state["v_idx"], use_actor=True, training=False
        )
        action_prob = Categorical(action_prob)
        action = action_prob.sample()
        log_p = action_prob.log_prob(action)
        action, log_p = action.cpu().numpy(), log_p.cpu().numpy()
        return (action[0], log_p[0]) if single else (action, log_p)
    
    def learn(self, states, actions, returns, next_states, p_idx, v_idx):
        """Model training.

        Args:
            batch (dict): The dictionary of a batch of experience. For example:
                {
                    "s": the dictionary of state,
                    "a": model actions in numpy array,
                    "R": the n-step accumulated reward,
                    "s"": the dictionary of the next state,
                }
            p_idx (int): The identity of the port doing the action.
            v_idx (int): The identity of the vessel doing the action.

        Returns:
            a_loss (float): action loss.
            c_loss (float): critic loss.
            e_loss (float): entropy loss.
            tot_norm (float): the L2 norm of the gradient.
        """
        states, actions, returns, next_states = self._preprocess(states, actions, returns, next_states)
        # Every port has a value.
        # values.shape: (batch, p_cnt)
        probs, values = self.model(states, p_idx=p_idx, v_idx=v_idx, use_actor=True, use_critic=True)
        distribution = Categorical(probs)
        log_prob = distribution.log_prob(actions)
        entropy_loss = distribution.entropy()

        _, values_ = self.model(next_states, use_critic=True)
        advantage = returns + self.config.value_discount * values_.detach() - values

        if self.config.entropy_factor != 0:
            # actor_loss = actor_loss* torch.log(entropy_loss + np.e)
            advantage[:, p_idx] += self.config.entropy_factor * entropy_loss.detach()

        actor_loss = -(log_prob * torch.sum(advantage, axis=-1).detach()).mean()
        critic_loss = torch.sum(advantage.pow(2), axis=1).mean()
        # torch.nn.utils.clip_grad_norm_(self._critic_model.parameters(),0.5)
        tot_loss = self.config.actor_loss_coefficient * actor_loss + critic_loss
        self.model.step(tot_loss)
        tot_norm = clip_grad.clip_grad_norm_(self.model.parameters(), 1)
        return actor_loss.item(), critic_loss.item(), entropy_loss.mean().item(), float(tot_norm)

    def _get_save_idx(self, fp_str):
        return int(fp_str.split(".")[0].split("_")[0])

    def save_model(self, pth, id):
        if not os.path.exists(pth):
            os.makedirs(pth)
        pth = os.path.join(pth, f"{id}_ac.pkl")
        torch.save(self.model.state_dict(), pth)

    def _set_gnn_weights(self, weights):
        for key in weights:
            if key in self.model.state_dict().keys():
                self.model.state_dict()[key].copy_(weights[key])
    
    def load_model(self, folder_pth, idx=-1):
        if idx == -1:
            fps = os.listdir(folder_pth)
            fps = [f for f in fps if "ac" in f]
            fps.sort(key=self._get_save_idx)
            ac_pth = fps[-1]
        else:
            ac_pth = f"{idx}_ac.pkl"
        pth = os.path.join(folder_pth, ac_pth)
        with open(pth, "rb") as fp:
            weights = torch.load(fp, map_location=self.device)
        self._set_gnn_weights(weights)

    def union(self, state) -> dict:
        """Union multiple graphs in CIM.

        Args:
            state (dict): State object. 
        Returns:
            result (dict): The dictionary that describes the graph.
        """
        single = len(state["p"].shape) == 3
        v = np.expand_dims(state["v"], 1) if single else state["v"]
        p = np.expand_dims(state["p"], 1) if single else state["p"]
        vo = np.expand_dims(state["vo"], 0) if single else state["vo"]
        po = np.expand_dims(state["po"], 0) if single else state["po"]
        pedge = np.expand_dims(state["pedge"], 0) if single else state["pedge"]
        vedge = np.expand_dims(state["vedge"], 0) if single else state["vedge"]
        ppedge = np.expand_dims(state["ppedge"], 0) if single else state["ppedge"]
        seq_mask = np.expand_dims(state["mask"], 0) if single else state["mask"]

        seq_len, batch, v_cnt, v_dim = v.shape
        _, _, p_cnt, p_dim = p.shape

        p = torch.from_numpy(p).float().to(self.device)
        po = torch.from_numpy(po).long().to(self.device)
        pedge = torch.from_numpy(pedge).float().to(self.device)
        v = torch.from_numpy(v).float().to(self.device)
        vo = torch.from_numpy(vo).long().to(self.device)
        vedge = torch.from_numpy(vedge).float().to(self.device)
        p2p = torch.from_numpy(self.config.p2p_adj).to(self.device)
        ppedge = torch.from_numpy(ppedge).float().to(self.device)
        seq_mask = torch.from_numpy(seq_mask).bool().to(self.device)

        batch_range = torch.arange(batch, dtype=torch.long).to(self.device)
        # vadj.shape: (batch*v_cnt, p_cnt*)
        vadj, vedge = self.flatten_embedding(vo, batch_range, vedge)
        # vmask.shape: (batch*v_cnt, p_cnt*)
        vmask = vadj == 0
        # vadj.shape: (p_cnt*, batch*v_cnt)
        vadj = vadj.transpose(0, 1)
        # vedge.shape: (p_cnt*, batch*v_cnt, e_dim)
        vedge = vedge.transpose(0, 1)

        padj, pedge = self.flatten_embedding(po, batch_range, pedge)
        pmask = padj == 0
        padj = padj.transpose(0, 1)
        pedge = pedge.transpose(0, 1)

        p2p_adj = p2p.repeat(batch, 1, 1)
        # p2p_adj.shape: (batch*p_cnt, p_cnt*)
        p2p_adj, ppedge = self.flatten_embedding(p2p_adj, batch_range, ppedge)
        # p2p_mask.shape: (batch*p_cnt, p_cnt*)
        p2p_mask = p2p_adj == 0
        # p2p_adj.shape: (p_cnt*, batch*p_cnt)
        p2p_adj = p2p_adj.transpose(0, 1)
        ppedge = ppedge.transpose(0, 1)

        return {
            "v": v,
            "p": p,
            "pe": {"edge": pedge, "adj": padj, "mask": pmask},
            "ve": {"edge": vedge, "adj": vadj, "mask": vmask},
            "ppe": {"edge": ppedge, "adj": p2p_adj, "mask": p2p_mask},
            "mask": seq_mask,
        }

    def _preprocess(self, states, actions, returns, next_states):
        states = self.union(states)
        actions = torch.from_numpy(actions).long().to(self.device)
        returns = torch.from_numpy(returns).float().to(self.device)
        next_states = self.union(next_states)
        return states, actions, returns, next_states

    @staticmethod
    def flatten_embedding(embedding, batch_range, edge=None):
        if len(embedding.shape) == 3:
            batch, x_cnt, y_cnt = embedding.shape
            addon = (batch_range * y_cnt).view(batch, 1, 1)
        else:
            seq_len, batch, x_cnt, y_cnt = embedding.shape
            addon = (batch_range * y_cnt).view(seq_len, batch, 1, 1)

        embedding_mask = embedding == 0
        embedding += addon
        embedding[embedding_mask] = 0
        ret = embedding.reshape(-1, embedding.shape[-1])
        col_mask = ret.sum(dim=0) != 0
        ret = ret[:, col_mask]
        if edge is None:
            return ret
        else:
            edge = edge.reshape(-1, *edge.shape[2:])[:, col_mask, :]
            return ret, edge
