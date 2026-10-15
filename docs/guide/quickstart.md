# Quickstart

This walk-through starts keel, creates a profile, runs an agent that uses a tool, follows its events, and stops a
run. You need Docker, `curl`, and either AWS credentials with Bedrock access or an OpenAI-compatible API key.

## 1. Start the service

From a clone of the repository:

```bash
KEEL_API_KEY=dev KEEL_API_KEY_TENANT=1 docker compose -f docker-compose.dev.yml up --build
```

This starts Postgres and keel on `127.0.0.1:8700`. keel calls Bedrock with the AWS profile in `~/.aws`
(`AWS_PROFILE`, default `default`; `AWS_REGION`, default `us-east-1`) and starts sandbox containers through the
host's Docker socket (see the warning in [sandbox.md](sandbox.md)).

To use an OpenAI-compatible provider instead, export `KEEL_OPENAI_API_KEY` before `up`; the compose file passes
it through (an empty key leaves the provider unregistered). Optionally export `KEEL_OPENAI_BASE_URL` (another
compatible server) and `KEEL_OPENAI_COMPAT=1` (strict servers). The `~/.aws` mount is only needed for Bedrock.

Without Docker you can run `pip install -e .` and then
`KEEL_API_KEY=dev KEEL_API_KEY_TENANT=1 KEEL_DATABASE_URL=sqlite:///keel.sqlite3 python -m keel`;
with `KEEL_SANDBOX=none` the agent
answers without tools.

Check it is up:

```bash
curl -s localhost:8700/healthz
# {"status":"ok"}
```

The rest uses these shell variables. Set `PROVIDER` and `MODEL` to what you use (`bedrock` with a Bedrock model
id, or `openai` with a model name); use a model id your account or endpoint exposes.

```bash
AUTH='Authorization: Bearer dev'
JSON='content-type: application/json'
API=localhost:8700/v1
PROVIDER=bedrock
MODEL=openai.gpt-6-luna
```

## 2. Create a profile

A profile says which models may be used, how its sandbox looks, and who the assistant is.

```bash
curl -s -X PUT $API/profiles/demo -H "$AUTH" -H "$JSON" -d '{
  "models": [{"provider": "'$PROVIDER'", "model": "'$MODEL'"}],
  "sandbox": {"image": "python:3.12-slim", "workdir": "/work"},
  "soul": "You are a concise assistant."
}'
```

Pull the image first so the first run does not wait on it (creating a sandbox times out after 300 s):

```bash
docker pull python:3.12-slim
```

## 3. Start a run

```bash
RUN=$(curl -s -X POST $API/profiles/demo/runs -H "$AUTH" -H "$JSON" -d '{
  "input": "Write a Python script that prints the first 10 squares, run it, and show the output.",
  "provider": "'$PROVIDER'", "model": "'$MODEL'", "session_id": "chat-1"
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo $RUN
```

The response is `{"run_id":"run_...","status":"queued","session_id":"chat-1"}`; the command keeps the `run_id` in `RUN`.

## 4. Follow the events

```bash
curl -N $API/profiles/demo/runs/$RUN/events -H "$AUTH"
```

You see `run.started`, then `message.delta` pieces, and for each tool call a `tool.started` and `tool.completed`
pair, ending with `run.completed` (or `run.failed` / `run.cancelled`). The stream closes after that. If your
connection drops, resume with `?after=<last seq>`.

Poll the run instead if you prefer: `curl -s $API/profiles/demo/runs/$RUN -H "$AUTH"` shows `status`, `output`
and `usage`.

## 5. Continue the conversation

Start another run with the same `session_id`. The session keeps its messages and its working directory, so files
written earlier are still there. A session accepts one run at a time (409 `session_busy` otherwise).

```bash
curl -s $API/profiles/demo/sessions/chat-1/messages -H "$AUTH"
```

## 6. Stop a run

Start a long task, then stop it:

```bash
RUN2=$(curl -s -X POST $API/profiles/demo/runs -H "$AUTH" -H "$JSON" -d '{
  "input": "Run a command that sleeps for 120 seconds.",
  "provider": "'$PROVIDER'", "model": "'$MODEL'", "session_id": "chat-2"
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
curl -s -X POST $API/profiles/demo/runs/$RUN2/stop -H "$AUTH"
```

The run moves to `stopping`, its command is killed, and the event stream ends with `run.cancelled`.

## Next

- [Configuration](configuration.md) for every setting.
- [HTTP API](api.md) for request and event shapes, and error codes.
- [Sandbox](sandbox.md) to tune the policy, and to add skills.
- [Providers](providers.md) to point keel at another model server.
