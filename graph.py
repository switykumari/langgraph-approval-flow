"""LangGraph workflow: collect -> analyze -> propose -> human approval -> finalize."""
import json
import os
import re
import sqlite3
from typing import TypedDict

from dotenv import load_dotenv
from groq import Groq
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

load_dotenv()
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_REVISIONS = 3  # stop redrafting after this many tries
llm = Groq()


# ---------- STATE: the data that travels through the whole workflow ----------
class TicketState(TypedDict):
    ticket: str      # customer message
    category: str    # billing / bug / account / other
    urgency: str     # low / medium / high
    draft: str       # current draft reply
    feedback: str    # reviewer feedback after a rejection
    revisions: int   # how many drafts were written
    status: str      # pending / approved / rejected / sent / not_sent
    trace: list      # short log of what happened


def ask_llm(system: str, user: str) -> str:
    resp = llm.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=800,
    )
    return (resp.choices[0].message.content or "").strip()


# ---------- NODES: each one is a step; it returns the parts of state it changes ----------
def collect(state: TicketState):
    ticket = state["ticket"].strip()
    return {"ticket": ticket, "trace": state["trace"] + ["collect: ticket received"]}


def analyze(state: TicketState):
    text = ask_llm(
        "You classify customer support tickets. Reply with JSON only, like "
        '{"category": "billing|bug|account|other", "urgency": "low|medium|high"}',
        state["ticket"],
    )
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0))
    except (AttributeError, json.JSONDecodeError):
        data = {}
    category = data.get("category", "other")
    urgency = data.get("urgency", "medium")
    return {
        "category": category,
        "urgency": urgency,
        "trace": state["trace"] + [f"analyze: {category}, {urgency} urgency"],
    }


def propose(state: TicketState):
    prompt = (
        f"Ticket:\n{state['ticket']}\n\n"
        f"Category: {state['category']}, urgency: {state['urgency']}"
    )
    if state["feedback"]:
        prompt += (
            f"\n\nThe previous draft was rejected. Reviewer feedback: {state['feedback']}"
            f"\nPrevious draft:\n{state['draft']}\nWrite an improved reply."
        )
    draft = ask_llm(
        "You are a polite support agent. Write a short reply (under 120 words). "
        "Do not invent order numbers, refunds or promises.",
        prompt,
    )
    n = state["revisions"] + 1
    return {
        "draft": draft,
        "revisions": n,
        "status": "pending",
        "trace": state["trace"] + [f"propose: draft #{n} written"],
    }


def approve(state: TicketState):
    # interrupt() PAUSES the graph here. The value passed in is shown to the human.
    # When the human answers, interrupt() returns that answer and the node continues.
    decision = interrupt(
        {
            "draft": state["draft"],
            "category": state["category"],
            "urgency": state["urgency"],
            "revision": state["revisions"],
        }
    )
    action = decision.get("action")
    if action == "approve":
        return {"status": "approved", "trace": state["trace"] + ["approve: human approved"]}
    if action == "edit":
        return {
            "draft": decision.get("text", state["draft"]),
            "status": "approved",
            "trace": state["trace"] + ["approve: human edited and approved"],
        }
    return {
        "status": "rejected",
        "feedback": decision.get("feedback", ""),
        "trace": state["trace"] + ["approve: human rejected"],
    }


def finalize(state: TicketState):
    if state["status"] == "approved":
        with open("sent_replies.txt", "a", encoding="utf-8") as f:
            f.write(f"--- [{state['category']}/{state['urgency']}] ---\n{state['draft']}\n\n")
        return {"status": "sent", "trace": state["trace"] + ["finalize: reply saved as sent"]}
    return {"status": "not_sent", "trace": state["trace"] + ["finalize: nothing sent"]}


# ---------- CONDITIONAL EDGE: decides the next node from the state ----------
def route_after_approval(state: TicketState):
    if state["status"] == "rejected" and state["revisions"] < MAX_REVISIONS:
        return "propose"   # redraft with the feedback
    return "finalize"


# ---------- BUILD THE GRAPH ----------
def build_graph(db_path: str = "checkpoints.db"):
    g = StateGraph(TicketState)
    g.add_node("collect", collect)
    g.add_node("analyze", analyze)
    g.add_node("propose", propose)
    g.add_node("approve", approve)
    g.add_node("finalize", finalize)

    g.add_edge(START, "collect")
    g.add_edge("collect", "analyze")
    g.add_edge("analyze", "propose")
    g.add_edge("propose", "approve")
    g.add_conditional_edges(
        "approve", route_after_approval, {"propose": "propose", "finalize": "finalize"}
    )
    g.add_edge("finalize", END)

    # SQLite checkpointer: saves the state after every step so we can pause/resume
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn))