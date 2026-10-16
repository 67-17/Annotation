"""Forward the web port of an existing experiment; never creates a new task."""
import argparse
import json
import os


def create_session(master):
    """Use the installed CLI's credential cache with either authentication API."""
    from determined.common import api
    from determined.common.api import authentication, certs

    cert = certs.default_load(master)
    login = getattr(authentication, "login_with_cache", None)
    if callable(login):
        # Determined 0.38.x stores authentication in the returned Session.
        return login(master_address=master, cert=cert)
    # Determined 0.22.x forwarding reads this module-level authentication object.
    auth = authentication.Authentication(master, cert=cert)
    authentication.cli_auth = auth
    return api.Session(master=master, user=auth.get_session_user(), auth=auth, cert=cert)


def diagnose_proxy(session, experiment):
    """Read only the selected experiment's port registration and resource addresses."""
    from determined.common.api import bindings

    config = experiment.config if isinstance(experiment.config, dict) else {}
    environment = config.get("environment") or {}
    result = {
        "experiment": experiment.id,
        "environment": {key: environment.get(key) for key in ("ports", "proxy_ports")},
        "trials": [],
        "active_allocations": [],
    }
    trials = bindings.get_GetExperimentTrials(session, experimentId=experiment.id).trials
    task_ids = set()
    for summary in sorted(trials, key=lambda trial: trial.id):
        trial = bindings.get_GetTrial(session, trialId=summary.id).trial
        task_ids.add(trial.taskId)
        result["trials"].append({"id": trial.id, "state": str(trial.state), "task_id": trial.taskId})
    tasks = bindings.get_GetTasks(session).allocationIdToSummary or {}
    for allocation_id, task in tasks.items():
        if task.taskId in task_ids:
            result["active_allocations"].append({"allocation_key": allocation_id, **task.to_json()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=int)
    parser.add_argument("--master", default=os.environ.get("DET_MASTER", "http://10.130.130.5:4096"))
    parser.add_argument("--port", type=int, default=8088, help="Local port (task port is 8088)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--logs-only", action="store_true", help="Read state and recent logs without opening a port")
    mode.add_argument("--diagnose", action="store_true", help="Read proxy registration and resource addresses; no port opened")
    parser.add_argument("--tail", type=int, default=60, help="Recent log lines for --logs-only")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.tail < 1:
        parser.error("tail must be positive")
    # lomond (used by det 0.22.2) ignores NO_PROXY for WebSockets.
    # This helper connects directly to the cluster; change only this process.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    from urllib.parse import urlsplit
    host = urlsplit(args.master).hostname
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(filter(None, [os.environ.get(key, ""), host]))

    # Reuse the installed CLI's authenticated WebSocket forwarding mechanism.
    from determined.common import api
    from determined.common.api import bindings
    from determined.cli import proxy
    from determined.cli.experiment import _follow_experiment_logs

    session = create_session(args.master)
    experiment = bindings.get_GetExperiment(session, experimentId=args.experiment).experiment
    state = getattr(experiment.state, "value", str(experiment.state))
    print(f"Experiment {args.experiment}: {state}", flush=True)
    if args.diagnose:
        print(json.dumps(diagnose_proxy(session, experiment), ensure_ascii=False, indent=2))
        return
    if args.logs_only:
        trials = bindings.get_GetExperimentTrials(session, experimentId=args.experiment).trials
        if not trials:
            print("No trial has been created yet.")
            return
        trial_id = min(t.id for t in trials)
        print(f"Trial {trial_id}; last {args.tail} log lines:", flush=True)
        api.pprint_logs(api.trial_logs(session, trial_id, tail=args.tail))
        return
    if experiment.state in (
        bindings.experimentv1State.COMPLETED,
        bindings.experimentv1State.CANCELED,
        bindings.experimentv1State.ERROR,
    ):
        parser.exit(1, f"Experiment {args.experiment} has stopped; no web service is running.\n"
                    f"Inspect startup logs: det -m {args.master} e logs {args.experiment} --tail 80\n")
    print("Waiting for a trial and opening the port forward…", flush=True)
    with proxy.tunnel_experiment(session, args.experiment, {args.port: 8088}):
        print(f"Port forward open: http://127.0.0.1:{args.port}/ on THIS machine. "
              "The app must finish starting before it responds. Keep this terminal open.", flush=True)
        print("Following experiment logs; Ctrl+C closes forwarding, not the experiment.", flush=True)
        _follow_experiment_logs(session, args.experiment)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Port forwarding closed; experiment was not canceled.")
