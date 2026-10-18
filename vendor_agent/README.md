# Liaison Vendor Agent

The company's front door. Answers questions from uploaded documents with citations, serves
learned expert answers from a knowledge base, checks every question and every outgoing answer
with the Compliance Guard, escalates to a human when it can't answer, and accepts expert answers
back into the KB only through a strict trust rule.

Speaks the [Chat Protocol](https://innovationlab.fetch.ai/resources/docs/agent-communication/chat-protocol-spec):
ASI:One reaches it directly as an external user; the Slack bot (Router) talks to it over the
same protocol as one more chat client.

## Run

```
pip install -r contracts/requirements.txt -r vendor_agent/requirements.txt -r requirements-dev.txt
pytest contracts/tests vendor_agent              # offline, no network, no API cost
python -m vendor_agent.smoke                     # local wiring check against fakes
set -a; . vendor_agent/.env; set +a; python -m vendor_agent.agent
```

See `vendor_agent/.env.example` for every variable, and `NOTES.md` for what each one is for and
what's still unverified.
