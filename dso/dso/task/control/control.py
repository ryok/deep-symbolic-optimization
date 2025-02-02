import gym

import numpy as np

import dso.task.control # Registers custom and third-party environments
from dso.program import Program, from_str_tokens
from dso.library import Library
from dso.functions import create_tokens
import dso.task.control.utils as U


REWARD_SEED_SHIFT = int(1e6) # Reserve the first million seeds for evaluation

# Pre-computed values for reward scale
REWARD_SCALE = {
    "CustomCartPoleContinuous-v0" : [0.0,1000.0],
    "MountainCarContinuous-v0" : [0.0,93.95],
    "Pendulum-v0" : [-1300.0,-147.56],
    "InvertedDoublePendulumBulletEnv-v0" : [0.0,9357.77],
    "InvertedPendulumSwingupBulletEnv-v0" : [0.0,891.34],
    "LunarLanderContinuous-v2" : [0.0,272.65],
    "HopperBulletEnv-v0" : [0.0,2741.86],
    "ReacherBulletEnv-v0" : [-5.0, 19.05],
    "BipedalWalker-v2" : [-60.0, 312.0]
}


def make_control_task(function_set, env, action_spec, algorithm=None,
    anchor=None, n_episodes_train=5, n_episodes_test=1000, success_score=None,
    protected=False, env_kwargs=None, fix_seeds=False, episode_seed_shift=0,
    reward_scale=True):
    """
    Factory function for episodic reward function of a reinforcement learning
    environment with continuous actions. This includes closures for the
    environment, an anchor model, and fixed symbolic actions.

    Parameters
    ----------

    function_set : list
        List of allowable functions.

    env : str
        Name of Gym environment, e.g. "Pendulum-v0" or "my_module:MyEnv-v0".

    action_spec : list
        List of action specifications: None, "anchor", or a list of tokens.

    algorithm : str or None
        Name of algorithm corresponding to anchor path, or None to use default
        anchor for given environment.

    anchor : str or None
        Path to anchor model, or None to use default anchor for given
        environment.

    n_episodes_train : int
        Number of episodes to run during training.

    n_episodes_test : int
        Number of episodes to run during testing.

    protected : bool
        Whether or not to use protected operators.

    env_kwargs : dict
        Dictionary of environment kwargs passed to gym.make().

    fix_seeds : bool
        If True, environment uses the first n_episodes_train seeds for reward
        and the next n_episodes_test seeds for evaluation. This makes the task
        deterministic.

    episode_seed_shift : int
        Training episode seeds start at episode_seed_shift * 100 +
        REWARD_SEED_SHIFT. This has no effect if fix_seeds == False.

    reward_scale : list or bool
        If list: list of [r_min, r_max] used to scale rewards. If True, use
        default values in REWARD_SCALE. If False, don't scale rewards.

    Returns
    -------

    See dso.task.task.make_task().
    """

    env_name = env
    if env_kwargs is None:
        env_kwargs = {}

    # Create the environment
    env = gym.make(env_name, **env_kwargs)

    # Determine reward scaling
    if isinstance(reward_scale, list):
        assert len(reward_scale) == 2, "Reward scale should be length 2: min, max."
        r_min, r_max = reward_scale
    elif reward_scale:
        if env_name in REWARD_SCALE:
            r_min, r_max = REWARD_SCALE[env_name]
        else:
            raise RuntimeError("{} has no default values for reward_scale. Use reward_scale=False or specify reward_scale=[r_min, r_max].".format(env_name))
    else:
        r_min = r_max = None

    # HACK: Wrap pybullet envs in TimeFeatureWrapper
    # TBD: Load the Zoo hyperparameters, including wrapper features, not just the model.
    # Note Zoo is not implemented as a package, which might make this tedious
    if "Bullet" in env_name:
        env = U.TimeFeatureWrapper(env)

    # Set the library (need to do this now in case there are symbolic actions)
    stochastic = not fix_seeds
    n_input_var = env.observation_space.shape[0]
    tokens = create_tokens(n_input_var, function_set, protected)
    library = Library(tokens)
    Program.library = library

    # Configuration assertions
    assert len(env.observation_space.shape) == 1, "Only support vector observation spaces."
    assert isinstance(env.action_space, gym.spaces.Box), "Only supports continuous action spaces."
    n_actions = env.action_space.shape[0]
    assert n_actions == len(action_spec), "Received specifications for {} action dimensions; expected {}.".format(len(action_spec), n_actions)
    assert len([v for v in action_spec if v is None]) <= 1, "No more than 1 action_spec element can be None."
    assert int(algorithm is None) + int(anchor is None) in [0, 2], "Either none or both of (algorithm, anchor) must be None."

    # Load the anchor model (if applicable)
    if "anchor" in action_spec:
        # Load custom anchor, if provided, otherwise load default
        if algorithm is not None and anchor is not None:
            U.load_model(algorithm, anchor)
        else:
            U.load_default_model(env_name)
        model = U.model
    else:
        model = None

    # Generate symbolic policies and determine action dimension
    symbolic_actions = {}
    action_dim = None
    for i, spec in enumerate(action_spec):

        # Action taken from anchor policy
        if spec == "anchor":
            continue

        # Action dimnension being learned
        if spec is None:
            action_dim = i

        # Pre-specified symbolic policy
        elif isinstance(spec, list) or isinstance(spec, str):
            str_tokens = spec
            p = from_str_tokens(str_tokens, optimize=False, skip_cache=True)
            symbolic_actions[i] = p

        else:
            assert False, "Action specifications must be None, a str/list of tokens, or 'anchor'."


    def get_action(p, obs):
        """Helper function to get an action from Program p according to obs,
        since Program.execute() requires 2D arrays but we only want 1D."""

        action = p.execute(np.array([obs]))[0]

        return action


    def run_episodes(p, n_episodes, evaluate):
        """Runs n_episodes episodes and returns each episodic reward."""

        # Run the episodes and return the average episodic reward
        r_episodes = np.zeros(n_episodes, dtype=np.float64) # Episodic rewards for each episode
        for i in range(n_episodes):

            # During evaluation, always use the same seeds
            if evaluate:
                env.seed(i)
            elif fix_seeds:
                env.seed(i + (episode_seed_shift * 100) + REWARD_SEED_SHIFT)
            obs = env.reset()
            done = False
            while not done:

                # Compute anchor actions
                if model is not None:
                    action, _ = model.predict(obs)
                else:
                    action = np.zeros(env.action_space.shape, dtype=np.float32)

                # Replace fixed symbolic actions
                for j, fixed_p in symbolic_actions.items():
                    action[j] = get_action(fixed_p, obs)

                # Replace symbolic action with current program
                if action_dim is not None:
                    action[action_dim] = get_action(p, obs)

                # Replace NaNs and clip infinites
                action[np.isnan(action)] = 0.0 # Replace NaNs with zero
                action = np.clip(action, env.action_space.low, env.action_space.high)

                obs, r, done, _ = env.step(action)
                r_episodes[i] += r

        return r_episodes


    def reward(p):

        # Run the episodes
        r_episodes = run_episodes(p, n_episodes_train, evaluate=False)

        # Return the mean
        r_avg = np.mean(r_episodes)

        # Scale rewards to [0, 1]
        if r_min is not None:
            r_avg = (r_avg - r_min) / (r_max - r_min)

        return r_avg


    def evaluate(p):

        # Run the episodes
        r_episodes = run_episodes(p, n_episodes_test, evaluate=True)

        # Compute eval statistics
        r_avg_test = np.mean(r_episodes)
        success_rate = np.mean(r_episodes >= success_score)
        success = success_rate == 1.0

        info = {
            "r_avg_test" : r_avg_test,
            "success_rate" : success_rate,
            "success" : success
        }
        return info

    # Define name for task, based on environment and learned action dimension
    name = env_name
    if action_dim is not None:
        name += "_a{}".format(action_dim)

    extra_info = {
        "symbolic_actions" : symbolic_actions
    }

    task = dso.task.Task(reward_function=reward,
                evaluate=evaluate,
                library=library,
                stochastic=stochastic,
                task_type='control',
                name=name,
                extra_info=extra_info)

    return task
