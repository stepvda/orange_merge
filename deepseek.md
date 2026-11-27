# Using DeepSeek through the gateway

The gateway relays DeepSeek's own API at `/ai/deepseek`. You call it with your OneVoice AI key (`ovai_…`) and
any OpenAI- or Anthropic-compatible client. The gateway swaps in the DeepSeek account key, which stays on the
gateway, so you never need a DeepSeek account of your own. Requests and replies, streamed or not, pass through
unchanged.

This is DeepSeek's paid service, billed to one shared account. It is **not** the local model at `/ai/v1`, which
also answers to the name `deepseek-chat` but is Qwen running on the Mac Studio.

The HTTP contract (every status code and field) is in [API.md](API.md#deepseek). This page is the how-to.

## 1. Get a key that may call DeepSeek

A key reaches DeepSeek only when its endpoint list **names** `deepseek`. A key set to "any endpoint" covers the
local endpoints only, so most existing keys get `403 scope_denied` here until an administrator adds it.

- **Admin page** (`https://one.witysk.org/ai/admin`, Keys tab): create a key, or edit an existing one, and tick
  **deepseek (paid)**.
- **CLI** on the Mac Studio:

  ```bash
  .venv/bin/python -m onevoice_ai.cli newkey --name "my-app" --endpoints deepseek
  ```

  Use `--endpoints chat,models,deepseek` for a key that should also reach the local model. The secret is shown
  once.

If the key has a model list, the DeepSeek model names you send must be in it too.

The examples below read the key from an environment variable:

```bash
export ONEVOICE_AI_KEY="ovai_..."
```

## 2. Base URLs

| Client | Base URL |
|---|---|
| OpenAI SDKs and OpenAI-compatible tools | `https://one.witysk.org/ai/deepseek` |
| Anthropic SDKs | `https://one.witysk.org/ai/deepseek/anthropic` |
| Scripts on the Mac Studio itself | `http://127.0.0.1:8100/ai/deepseek` (or `…/anthropic`) |

Everything after the base URL is DeepSeek's own path: `/chat/completions`, `/models`, `/user/balance`,
`/beta/completions`, `/anthropic/v1/messages`. Send the key as `Authorization: Bearer ovai_…` or
`X-API-Key: ovai_…`. The SDKs do this for you.

## 3. Models

`GET /models` lists what DeepSeek offers now. On 27 November 2026 it listed:

| Model | Name | Input | Context | Max output |
|---|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash | text, images | 1,048,576 | 393,216 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro | text | 1,048,576 | 393,216 |

Both models **think by default**: the reply carries the model's reasoning in `reasoning_content` next to the
answer in `content`. The older names still work. `deepseek-chat` is answered by `deepseek-flash` without
thinking, and `deepseek-reasoner` by `deepseek-flash` with thinking.

- **Thinking off:** send `"thinking": {"type": "disabled"}`. This is faster and cheaper for simple requests.
- **Effort:** `"reasoning_effort"` takes `low`, `high` (the default) or `max`.
- **Cost:** thinking tokens are billed as output. In testing, a haiku from `deepseek-v4-pro` with thinking on
  cost 500 output tokens.

## 4. Examples

Every example below was run against the gateway and the live DeepSeek API.

### curl

```bash
BASE=https://one.witysk.org/ai/deepseek

# The models DeepSeek offers now
curl -s "$BASE/models" -H "Authorization: Bearer $ONEVOICE_AI_KEY"

# One answer
curl -s "$BASE/chat/completions" \
  -H "Authorization: Bearer $ONEVOICE_AI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Explain DNS in two sentences."}],
        "thinking": {"type": "disabled"}
      }'

# The same, streamed (-N: print each event as it arrives)
curl -sN "$BASE/chat/completions" \
  -H "Authorization: Bearer $ONEVOICE_AI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Explain DNS in two sentences."}],
        "thinking": {"type": "disabled"},
        "stream": true,
        "stream_options": {"include_usage": true}
      }'

# The account's balance (shared by every key)
curl -s "$BASE/user/balance" -H "Authorization: Bearer $ONEVOICE_AI_KEY"
```

### Python, OpenAI SDK

`pip install openai`

**One answer**, first with thinking and then without:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="https://one.witysk.org/ai/deepseek",
    api_key=os.environ["ONEVOICE_AI_KEY"],
)

reply = client.chat.completions.create(
    model="deepseek-flash",
    messages=[
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "Explain DNS in two sentences."},
    ],
)
message = reply.choices[0].message
print("thinking:", getattr(message, "reasoning_content", None))
print("answer:  ", message.content)
print("tokens:  ", reply.usage.prompt_tokens, "in,", reply.usage.completion_tokens, "out")

# No thinking: faster and cheaper for simple requests. `thinking` is DeepSeek's own field,
# so the SDK passes it in extra_body.
reply = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "Translate 'good morning' into Dutch."}],
    extra_body={"thinking": {"type": "disabled"}},
)
print(reply.choices[0].message.content)
```

**Streaming.** Ask for `include_usage`, so the last chunk carries the token counts. Without it, a streamed
call's tokens are missing from the usage log and from your key's daily token budget.

```python
import os

from openai import OpenAI

client = OpenAI(base_url="https://one.witysk.org/ai/deepseek", api_key=os.environ["ONEVOICE_AI_KEY"])

stream = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[{"role": "user", "content": "Write a haiku about a lighthouse."}],
    stream=True,
    stream_options={"include_usage": True},   # the last chunk carries the token counts
)
for chunk in stream:
    if chunk.choices:
        delta = chunk.choices[0].delta
        if getattr(delta, "reasoning_content", None):
            continue                          # the model's thinking, streamed before the answer
        if delta.content:
            print(delta.content, end="", flush=True)
    if chunk.usage:
        print(f"\n[{chunk.usage.prompt_tokens} in, {chunk.usage.completion_tokens} out]")
```

**Tool calls.** The model asks for a function, your code runs it, and you send the result back until the model
answers in text:

```python
import json
import os

from openai import OpenAI

client = OpenAI(base_url="https://one.witysk.org/ai/deepseek", api_key=os.environ["ONEVOICE_AI_KEY"])

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]


def get_weather(city: str) -> dict:
    return {"city": city, "temp_c": 11, "sky": "overcast"}      # your real lookup goes here


messages = [{"role": "user", "content": "Do I need a coat in Brussels today?"}]
while True:
    reply = client.chat.completions.create(model="deepseek-flash", messages=messages, tools=tools)
    message = reply.choices[0].message
    # Append the assistant turn as it came back: its tool_calls, and its reasoning_content when thinking is on.
    messages.append(message.model_dump(exclude_none=True))
    if not message.tool_calls:
        break
    for call in message.tool_calls:
        result = get_weather(**json.loads(call.function.arguments))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})

print(message.content)
```

### JavaScript, OpenAI SDK

`npm install openai`, then run as an ES module (`chat.mjs`):

```js
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "https://one.witysk.org/ai/deepseek",
  apiKey: process.env.ONEVOICE_AI_KEY,
});

const stream = await client.chat.completions.create({
  model: "deepseek-flash",
  messages: [{ role: "user", content: "Give me three names for a bakery." }],
  thinking: { type: "disabled" },        // DeepSeek's own field (TypeScript: cast the params)
  stream: true,
  stream_options: { include_usage: true },
});

for await (const chunk of stream) {
  const text = chunk.choices[0]?.delta?.content;
  if (text) process.stdout.write(text);
  if (chunk.usage) console.log(`\n[${chunk.usage.prompt_tokens} in, ${chunk.usage.completion_tokens} out]`);
}
```

### Python, Anthropic SDK

DeepSeek also speaks Anthropic's Messages API. Point the SDK at the `/anthropic` base URL. It sends your key as
`X-API-Key`, which the gateway accepts.

`pip install anthropic`

```python
import os

import anthropic

client = anthropic.Anthropic(
    base_url="https://one.witysk.org/ai/deepseek/anthropic",
    api_key=os.environ["ONEVOICE_AI_KEY"],
)

message = client.messages.create(
    model="deepseek-flash",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Name the three largest moons of Jupiter."}],
)
for block in message.content:
    if block.type == "text":
        print(block.text)
print(message.usage.input_tokens, "in,", message.usage.output_tokens, "out")
```

### Other OpenAI-compatible tools

Any tool with an "OpenAI-compatible" provider needs three settings: base URL
`https://one.witysk.org/ai/deepseek`, your `ovai_…` key as the API key, and a model id such as `deepseek-flash`.
For Kilo Code, this is a second provider next to the local one in [KILO_CODE.md](KILO_CODE.md), with this
`baseURL`.

## 5. Limits and what is recorded

- **Request size:** at most 4 MB per request body (`server.max_body_mb`), images included. A larger one gets
  `413`.
- **Your key's limits** apply: requests per minute and tokens per day. There is no local queue, because the
  request never touches the Mac Studio's GPU. DeepSeek's own rate limits come back as DeepSeek's `429`.
- **Timeouts:** the gateway waits up to 10 minutes between two bytes of DeepSeek's reply, which is enough for
  long thinking. Keep your client's timeout at least that long. The OpenAI SDK's default is 10 minutes.
- **Usage log:** one row per call in the admin page's Usage tab, with endpoint `deepseek`, your key, the model,
  the status, the timings and the token counts. Prompts and answers are never logged.

## 6. Errors

| Status | `error.code` | What it means and what to do |
|---|---|---|
| 401 | `invalid_api_key` | The `ovai_` key is missing, wrong or revoked. |
| 403 | `scope_denied` | The key's endpoint list does not name `deepseek`, or its model list leaves out the model you sent. Ask an administrator. |
| 404 | `not_found` | Not a DeepSeek path (for example one containing `..`). |
| 413 | `body_too_large` | The request body is over 4 MB. |
| 429 | `rate_limited` | Your key's per-minute or per-day limit. Wait for `Retry-After`. |
| 400, 422, 429, 5xx | DeepSeek's own | DeepSeek refused the request itself, and its error is passed through as written. For example, an unknown `reasoning_effort` gets `422`. |
| 502 | `deepseek_auth_failed`, `deepseek_insufficient_balance` | DeepSeek refused the gateway's account, not your key. Tell an administrator. |
| 502, 504 | `deepseek_unreachable` | DeepSeek did not answer, or the reply broke off. Retry. |
| 503 | `deepseek_not_configured` | The gateway has no DeepSeek key. |

If a streamed reply breaks off after it has started, the stream ends with a final event
`data: {"error": {"code": "deepseek_unreachable", …}}`.
