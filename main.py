"""Run the approval workflow from the terminal."""
import sys
import uuid

from langgraph.types import Command

from graph import build_graph

DEFAULT_TICKET = (
    "Hi, I was charged twice for my subscription this month and I need "
    "this fixed today. Please help."
)


def ask_human(payload: dict) -> dict:
    print("\n" + "=" * 60)
    print(f"DRAFT #{payload['revision']}  [{payload['category']} / {payload['urgency']}]")
    print("=" * 60)
    print(payload["draft"])
    print("=" * 60)
    choice = input("(a)pprove, (r)eject, (e)dit, (q)uit and resume later? ").strip().lower()
    if choice == "q":
        print("Paused. State is saved in checkpoints.db.")
        sys.exit(0)
    if choice == "a":
        return {"action": "approve"}
    if choice == "e":
        return {"action": "edit", "text": input("Type the new reply: ")}
    return {"action": "reject", "feedback": input("What should be changed? ")}


def review_loop(app, config, payload):
    """Keep asking the human until the graph finishes."""
    while True:
        decision = ask_human(payload)
        result = app.invoke(Command(resume=decision), config)
        if "__interrupt__" in result:        # graph paused again (after a redraft)
            payload = result["__interrupt__"][0].value
        else:
            return result


def main():
    app = build_graph()

    if len(sys.argv) > 2 and sys.argv[1] == "--resume":
        thread_id = sys.argv[2]
        config = {"configurable": {"thread_id": thread_id}}
        state = app.get_state(config)
        if not state.next:
            print("Nothing to resume for that thread id.")
            return
        payload = state.tasks[0].interrupts[0].value
        final = review_loop(app, config, payload)
    else:
        ticket = " ".join(sys.argv[1:]) or DEFAULT_TICKET
        thread_id = str(uuid.uuid4())[:8]
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Thread id: {thread_id}  (use it with --resume if you quit)")
        initial = {
            "ticket": ticket, "category": "", "urgency": "", "draft": "",
            "feedback": "", "revisions": 0, "status": "pending", "trace": [],
        }
        result = app.invoke(initial, config)
        if "__interrupt__" in result:
            final = review_loop(app, config, result["__interrupt__"][0].value)
        else:
            final = result

    print("\nFINAL STATUS:", final["status"])
    print("TRACE:")
    for step in final["trace"]:
        print("  -", step)


if __name__ == "__main__":
    main()