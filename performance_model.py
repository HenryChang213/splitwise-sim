import math
import os

from abc import ABC, abstractmethod

import pandas as pd

from hydra.utils import get_original_cwd
from scipy.interpolate import interp1d, LinearNDInterpolator
from collections import Counter
import numpy as np

from task import TaskType, PromptTask, TokenTask


performance_model = None


class PerformanceModel(ABC):
    """
    PerformanceModel helps estimate the duration of tasks or iterations,
    under given hardware, model, and parallelism configurations.
    Abstract class that must be subclassed.
    """
    def __init__(self):
        global performance_model
        performance_model = self

    @abstractmethod
    def get_duration(self, task, batch, instance, *args, **kwargs):
        """
        Returns the execution time of the task.
        """
        raise NotImplementedError

    @abstractmethod
    def get_iteration_duration(self, batch, instance, *args, **kwargs):
        """
        Returns the execution time of a contiguous iteration.
        """
        raise NotImplementedError


class ConstantPerformanceModel(PerformanceModel):
    """
    PerformanceModel that returns a constant value regardless of other parameters.
    Used for testing purposes.
    """
    def __init__(self, prompt_time, token_time):
        super().__init__()
        self.prompt_time = prompt_time
        self.token_time = token_time

    def get_duration(self, task, batch, instance, *args, **kwargs):
        if task.task_type == TaskType.PROMPT:
            return self.prompt_time
        elif task.task_type == TaskType.TOKEN:
            return self.token_time
        else:
            raise NotImplementedError

    def get_iteration_duration(self, batch, instance, *args, **kwargs):
        raise NotImplementedError


class DatabasePerformanceModel(PerformanceModel):
    """
    PerformanceModel based on a CSV database of characterization runs.
    Interpolates between data points and updates the database correspondingly.
    The underlying predictor could be changed for different interpolation strategies.
    """
    def __init__(self, db_path):
        super().__init__()
        self.db = pd.read_csv(os.path.join(get_original_cwd(), db_path),
                              dtype={"model": "category", "hardware": "category"})

        # ensure the database has the correct columns
        # and remove extraneous columns
        self.db = self.db[["model",
                           "hardware",
                           "tensor_parallel",
                           "prompt_size",
                           "batch_size",
                           "token_size",
                           "prompt_time",
                           "token_time"]]

        # convert to seconds
        self.db["prompt_time"] = self.db["prompt_time"] / 1000
        self.db["token_time"] = self.db["token_time"] / 1000

        self.init_predictor()

    def init_predictor(self):
        """
        Predict using number of tokens in the batch.
        """
        self.prompt_time_predictors = {}
        self.token_time_predictors = {}
        self.prompt_time_cache = {}
        self.token_time_cache = {}

        for model in self.db["model"].unique():
            for hardware in self.db["hardware"].unique():
                for tensor_parallel in self.db["tensor_parallel"].unique():
                    mask = (self.db["model"] == model) & \
                            (self.db["hardware"] == hardware) & \
                            (self.db["tensor_parallel"] == tensor_parallel)
                    db_subset = self.db[mask].copy()
                    if len(db_subset) == 0:
                        continue
                    db_subset["batch_tokens"] = db_subset["prompt_size"] * db_subset["batch_size"]
                    x = db_subset[["batch_tokens", "prompt_time"]].groupby("batch_tokens").median().index
                    y = db_subset[["batch_tokens", "prompt_time"]].groupby("batch_tokens").median()["prompt_time"]
                    self.prompt_time_predictors[(model, hardware, tensor_parallel)] = interp1d(
                                                                    x, y, fill_value="extrapolate")
                    x = db_subset[["batch_tokens", "token_time"]].groupby("batch_tokens").median().index
                    y = db_subset[["batch_tokens", "token_time"]].groupby("batch_tokens").median()["token_time"]
                    self.token_time_predictors[(model, hardware, tensor_parallel)] = interp1d(
                                                                    x, y, fill_value="extrapolate")

    def _match(self, **kwargs):
        """
        Returns a boolean mask for the database from kwargs.
        """
        mask = True
        for k, v in kwargs.items():
            mask &= (self.db[k] == v)
        return mask

    def predict_new_row(self, **kwargs):
        """
        Predicts the prompt and token time for a new row.
        Inserts the new row into the database.
        """
        model = kwargs["model"]
        hardware = kwargs["hardware"]
        tensor_parallel = kwargs["tensor_parallel"]
        batch_tokens = kwargs["batch_tokens"]
        new_row = pd.DataFrame(kwargs, index=[0])

        prompt_time = self.prompt_time_predictors[(model, hardware, tensor_parallel)](batch_tokens)
        token_time = self.token_time_predictors[(model, hardware, tensor_parallel)](batch_tokens)

        new_row["prompt_time"] = prompt_time
        new_row["token_time"] = token_time
        self.db = pd.concat([self.db, new_row], ignore_index=True)
        return new_row

    def get_prompt_time(self, **kwargs):
        """
        Returns the prompt time from the database.
        """
        prompt_time = self.db[self._match(**kwargs)]["prompt_time"].median()
        # if not found, predict
        if math.isnan(prompt_time):
            new_row = self.predict_new_row(**kwargs)
            prompt_time = new_row["prompt_time"][0]
        return prompt_time

    def get_token_time(self, **kwargs):
        """
        Returns the prompt time from the database.
        """
        token_time = self.db[self._match(**kwargs)]["token_time"].median()
        # if not found, predict
        if math.isnan(token_time):
            new_row = self.predict_new_row(**kwargs)
            token_time = new_row["token_time"][0]
        return token_time

    def get_duration(self,
                     task,
                     batch,
                     instance,
                     *args,
                     **kwargs):
        model = instance.model.name
        hardware = instance.processors[0].name
        pipeline_parallel = instance.model.parallelism.pipeline_parallelism
        tensor_parallel = instance.model.parallelism.tensor_parallelism
        if task.task_type == TaskType.PROMPT:
            prompt_size = task.request.prompt_size
            token_size = task.request.token_size
            batch_size = len(batch)
            prompt_time = self.get_prompt_time(model=model,
                                               hardware=hardware,
                                               tensor_parallel=tensor_parallel,
                                               prompt_size=prompt_size,
                                               batch_size=batch_size,
                                               token_size=token_size,
                                               batch=batch)
            return prompt_time
        elif task.task_type == TaskType.TOKEN:
            prompt_size = task.request.prompt_size
            token_size = task.request.token_size
            batch_size = len(batch)
            token_time = self.get_token_time(model=model,
                                             hardware=hardware,
                                             tensor_parallel=tensor_parallel,
                                             prompt_size=prompt_size,
                                             batch_size=batch_size,
                                             token_size=token_size,
                                             batch=batch)
            return token_time * task.token_size
        else:
            raise NotImplementedError

    def get_iteration_duration(self,
                               batch,
                               instance,
                               *args,
                               **kwargs):
        """
        Note: assumes that prompts are always processed fully.
        i.e., we currently do not support prompt chunking.
        """
        model = instance.model.name
        hardware = instance.processors[0].name
        pipeline_parallel = instance.model.parallelism.pipeline_parallelism
        tensor_parallel = instance.model.parallelism.tensor_parallelism

        prompt_tasks = []
        token_tasks = []
        batch_tokens = 0
        for task in batch:
            if isinstance(task, PromptTask):
                prompt_tasks.append(task)
                batch_tokens += task.request.prompt_size
            elif isinstance(task, TokenTask):
                token_tasks.append(task)
                batch_tokens += 1
            else:
                raise NotImplementedError

        iteration_time = None
        cache_key = (model, hardware, tensor_parallel, batch_tokens)
        predictors_key = (model, hardware, tensor_parallel)

        if len(prompt_tasks) == len(batch):
            iteration_time = self.prompt_time_cache.get(cache_key)
            if iteration_time is None:
                iteration_time = float(self.prompt_time_predictors[predictors_key](batch_tokens))
                self.prompt_time_cache[cache_key] = float(iteration_time)
        elif len(token_tasks) == len(batch):
            iteration_time = self.token_time_cache.get(cache_key)
            if iteration_time is None:
                iteration_time = float(self.token_time_predictors[predictors_key](batch_tokens))
                self.token_time_cache[cache_key] = float(iteration_time)
        else:
            iteration_time = self.prompt_time_cache.get(cache_key)
            if iteration_time is None:
                iteration_time = float(self.prompt_time_predictors[predictors_key](batch_tokens))
                self.prompt_time_cache[cache_key] = float(iteration_time)
            iteration_time *= 1.1

        assert iteration_time > 0
        return iteration_time


def get_duration(*args, **kwargs):
    """
    Returns the execution time of the task.
    """
    return performance_model.get_duration(*args, **kwargs)


def get_iteration_duration(*args, **kwargs):
    """
    Returns the execution time of a contiguous iteration.
    """
    return performance_model.get_iteration_duration(*args, **kwargs)


class DatabasePerformanceModelLLMCompass(PerformanceModel):
    """
    PerformanceModel based on a CSV database of characterization runs.
    Interpolates between data points and updates the database correspondingly.
    The underlying predictor could be changed for different interpolation strategies.
    """

    def __init__(self, db_path):
        super().__init__()
        self.db = {
            "prompt": pd.read_csv(
                os.path.join(get_original_cwd(), db_path, "results_prefill.csv"),
                dtype={"model": "category", "hardware": "category"},
            ),
            "token": pd.read_csv(
                os.path.join(get_original_cwd(), db_path, "results_decoding.csv"),
                dtype={"model": "category", "hardware": "category"},
            ),
        }

        # ensure the database has the correct columns
        # and remove extraneous columns
        self.db["prompt"] = self.db["prompt"][
            [
                "model",
                "hardware",
                "tp",
                "batch_size",
                "prompt_size",
                "prompt_time",
                "layer_time",
                "BatchedMatmul",
                "Softmax",
            ]
        ]
        self.db["prompt"]["layer_count"] = (
            self.db["prompt"]["prompt_time"] / self.db["prompt"]["layer_time"]
        ).round()
        self.db["token"] = self.db["token"][
            [
                "model",
                "hardware",
                "tp",
                "batch_size",
                "prompt_plus_token_size",
                "token_time",
                "layer_time",
                "BatchedMatmul",
                "Softmax",
            ]
        ]
        self.db["token"]["layer_count"] = (
            self.db["token"]["token_time"] / self.db["token"]["layer_time"]
        ).round()
        assert (
            self.db["prompt"]["layer_count"].min()
            == self.db["prompt"]["layer_count"].max()
        )
        assert (
            self.db["token"]["layer_count"].min()
            == self.db["token"]["layer_count"].max()
        )
        self.db["prompt"]["attention_time"] = (
            self.db["prompt"]["BatchedMatmul"] + self.db["prompt"]["Softmax"]
        ) * self.db["prompt"]["layer_count"]
        self.db["prompt"]["linear_time"] = (
            self.db["prompt"]["prompt_time"] - self.db["prompt"]["attention_time"]
        )
        self.db["token"]["attention_time"] = (
            self.db["token"]["BatchedMatmul"] + self.db["token"]["Softmax"]
        ) * self.db["token"]["layer_count"]
        self.db["token"]["linear_time"] = (
            self.db["token"]["token_time"] - self.db["token"]["attention_time"]
        )

        self.init_predictor()

    def init_predictor(self):
        """
        Predict using number of tokens in the batch.
        """
        self.prompt_time_predictors = {}
        self.token_time_predictors = {}
        self.prompt_time_cache = {}
        self.token_time_cache = {}

        # prompt time predictor
        for model in self.db["prompt"]["model"].unique():
            for hardware in self.db["prompt"]["hardware"].unique():
                for tensor_parallel in self.db["prompt"]["tp"].unique():
                    mask = (
                        (self.db["prompt"]["model"] == model)
                        & (self.db["prompt"]["hardware"] == hardware)
                        & (self.db["prompt"]["tp"] == tensor_parallel)
                    )
                    db_subset = self.db["prompt"][mask].copy()
                    if len(db_subset) == 0:
                        continue

                    db_subset["b_s_s"] = (
                        db_subset["batch_size"]
                        * db_subset["prompt_size"]
                        * db_subset["prompt_size"]
                    )
                    db_subset["b_s"] = (
                        db_subset["batch_size"] * db_subset["prompt_size"]
                    )

                    self.prompt_time_predictors[(model, hardware, tensor_parallel)] = {
                        "attention": interp1d(
                            db_subset["b_s_s"],
                            db_subset["attention_time"],
                            kind="linear",
                            fill_value=np.nan,
                        ),
                        "linear": interp1d(
                            db_subset["b_s"],
                            db_subset["linear_time"],
                            kind="linear",
                            fill_value=np.nan,
                        ),
                    }

        # token time predictor
        for model in self.db["token"]["model"].unique():
            for hardware in self.db["token"]["hardware"].unique():
                for tensor_parallel in self.db["token"]["tp"].unique():
                    mask = (
                        (self.db["token"]["model"] == model)
                        & (self.db["token"]["hardware"] == hardware)
                        & (self.db["token"]["tp"] == tensor_parallel)
                    )
                    db_subset = self.db["token"][mask].copy()
                    if len(db_subset) == 0:
                        continue

                    db_subset["b_s"] = (
                        db_subset["batch_size"] * db_subset["prompt_plus_token_size"]
                    )

                    self.token_time_predictors[(model, hardware, tensor_parallel)] = {
                        "attention": LinearNDInterpolator(
                            list(
                                zip(
                                    db_subset["batch_size"],
                                    db_subset["prompt_plus_token_size"],
                                )
                            ),
                            db_subset["attention_time"],
                            fill_value=np.nan,
                        ),
                        # "attention": interp1d(
                        #     db_subset["b_s"],
                        #     db_subset["attention_time"],
                        #     kind="linear",
                        #     fill_value=np.nan,
                        # ),
                        "linear": interp1d(
                            db_subset["batch_size"],
                            db_subset["linear_time"],
                            kind="linear",
                            fill_value=np.nan,
                        ),
                    }

    def _match(self, stage: str, **kwargs):
        """
        Returns a boolean mask for the database from kwargs.
        """
        mask = True
        for k, v in kwargs.items():
            mask &= self.db[stage][k] == v
        return mask

    def get_duration(self, task, batch, instance, *args, **kwargs):
        """
        Returns the execution time of the task.
        """
        raise NotImplementedError

    def get_iteration_duration(self, batch, instance, *args, **kwargs):
        """
        Note: assumes that prompts are always processed fully.
        i.e., we currently do not support prompt chunking.
        """
        model = instance.model.name
        hardware = instance.processors[0].name
        tensor_parallel = instance.model.parallelism.tensor_parallelism

        prompt_tasks = []
        token_tasks = []
        prompt_b_s_s = 0  # for prompt attention
        prompt_b_s = 0  # for prompt linear
        token_b_s = 0  # for token attention
        token_b = 0  # for token linear
        token_s_list = []
        for task in batch:
            if isinstance(task, PromptTask):
                prompt_tasks.append(task)
                prompt_b_s_s += task.request.prompt_size * task.request.prompt_size
                prompt_b_s += task.request.prompt_size
            elif isinstance(task, TokenTask):
                token_tasks.append(task)
                token_b_s += task.request.processed_tokens
                token_b += 1
                token_s_list.append(task.request.processed_tokens)
            else:
                raise NotImplementedError

        iteration_time = 0

        predictors_key = (model, hardware, tensor_parallel)

        if prompt_b_s > 0 and token_b_s > 0:
            iteration_time = float(
                self.prompt_time_predictors[predictors_key]["attention"](prompt_b_s_s)
            ) + float(
                self.prompt_time_predictors[predictors_key]["linear"](
                    prompt_b_s + token_b
                )
            )

            # iteration_time += float(
            #     self.token_time_predictors[predictors_key]["attention"](token_b_s)
            # )

            iteration_time += float(
                self.token_time_predictors[predictors_key]["attention"](
                    token_b, token_b_s / token_b
                )
            )

            # counts = Counter(token_s_list)
            # for token_s, count in counts.items():
            #     iteration_time += float(
            #         self.token_time_predictors[predictors_key]["attention"](
            #             count, token_s
            #         )
            #     )

        elif prompt_b_s > 0:
            iteration_time = float(
                self.prompt_time_predictors[predictors_key]["attention"](prompt_b_s_s)
            ) + float(self.prompt_time_predictors[predictors_key]["linear"](prompt_b_s))

        elif token_b_s > 0:

            iteration_time = float(
                self.token_time_predictors[predictors_key]["linear"](token_b)
            )

            # iteration_time += float(
            #     self.token_time_predictors[predictors_key]["attention"](token_b_s)
            # )
            iteration_time += float(
                self.token_time_predictors[predictors_key]["attention"](
                    token_b, token_b_s / token_b
                )
            )

            # counts = Counter(token_s_list)
            # for token_s, count in counts.items():
            #     iteration_time += float(
            #         self.token_time_predictors[predictors_key]["attention"](
            #             count, token_s
            #         )
            #     )

        assert (
            iteration_time > 0
        ), f"iteration_time: {iteration_time}, prompt_b_s_s: {prompt_b_s_s}, prompt_b_s: {prompt_b_s}, token_b_s: {token_b_s}, token_b: {token_b}, token_s_list: {Counter(token_s_list)}"
        return iteration_time
