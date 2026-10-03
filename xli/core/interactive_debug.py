#!/usr/bin/env python3
"""
XLI Interactive Debug v4 — Dialog: questions → answers → exact fix
"""

from dataclasses import dataclass, field
from datetime import datetime

from xli.core.logger import StructuredLogger
from xli.providers.base import get_provider

logger = StructuredLogger("xli.debug_interactive")


@dataclass
class DebugSession:
    """Interactive debugging session"""
    session_id: str
    error: str
    code: str
    questions: list[dict] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    status: str = "active"  # active, resolved, abandoned


class InteractiveDebugger:
    """Dialog-based interactive debugging"""

    def __init__(self):
        self.sessions: dict[str, DebugSession] = {}
        logger.log_structured("INFO", "debug_interactive", "Initialized")

    def start_debug(self, error: str, code: str) -> str:
        """Start new debug session"""
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        session = DebugSession(
            session_id=session_id,
            error=error,
            code=code
        )

        self.sessions[session_id] = session
        logger.log_structured("INFO", "debug_interactive",
                             f"Session started: {session_id}")

        return session_id

    async def ask_question(self, session_id: str) -> str | None:
        """Generate next question for session"""
        if session_id not in self.sessions:
            return None

        session = self.sessions[session_id]

        # Build context from previous Q&A
        context = f"""Error: {session.error}

Code:
{session.code[:1000]}

Previous Q&A:
"""
        for q, a in zip(session.questions, session.answers):
            context += f"Q: {q.get('text', '')}\nA: {a}\n"

        prompt = f"""{context}

Generate ONE specific question to narrow down the bug. 
Question should be answerable with yes/no or short text.

Return ONLY the question text."""

        try:
            provider = get_provider()
            question = await provider.chat([
                {"role": "system", "content": "Debug assistant. One question at a time."},
                {"role": "user", "content": prompt}
            ], temperature=0.4)

            question_text = question.strip()

            session.questions.append({
                "text": question_text,
                "timestamp": datetime.now().isoformat()
            })

            logger.log_structured("DEBUG", "debug_interactive",
                                 f"Question: {question_text[:50]}")

            return question_text

        except Exception as e:
            logger.log_error("debug_interactive", "Question generation failed", exc=e)
            return "What specific error message do you see?"

    def process_answer(self, session_id: str, answer: str) -> bool:
        """Process user answer"""
        if session_id not in self.sessions:
            return False

        session = self.sessions[session_id]
        session.answers.append(answer)

        logger.log_structured("DEBUG", "debug_interactive",
                             f"Answer received: {answer[:50]}")

        # Check if we have enough info (3-5 Q&A pairs)
        if len(session.answers) >= 3:
            return True  # Ready to suggest fix

        return False  # Need more questions

    async def suggest_fix(self, session_id: str) -> str:
        """Suggest fix based on session history"""
        if session_id not in self.sessions:
            return "Session not found"

        session = self.sessions[session_id]

        # Build full context
        context = f"""Error: {session.error}

Code:
{session.code[:1500]}

Debug Session:
"""
        for i, (q, a) in enumerate(zip(session.questions, session.answers)):
            context += f"\nQ{i+1}: {q.get('text', '')}\nA: {a}\n"

        prompt = f"""{context}

Based on this debug session, provide the EXACT fix.
Return ONLY the corrected code or specific change needed."""

        try:
            provider = get_provider()
            fix = await provider.chat([
                {"role": "system", "content": "Provide exact code fixes."},
                {"role": "user", "content": prompt}
            ], temperature=0.3)

            session.status = "resolved"
            logger.log_structured("INFO", "debug_interactive",
                                 f"Fix suggested for {session_id}")

            return fix

        except Exception as e:
            logger.log_error("debug_interactive", "Fix suggestion failed", exc=e)
            return f"Error generating fix: {e}"

    def get_session_history(self, session_id: str) -> DebugSession | None:
        """Get session history"""
        return self.sessions.get(session_id)

    def list_sessions(self) -> list[dict]:
        """List all sessions"""
        return [
            {
                "id": s.session_id,
                "status": s.status,
                "questions": len(s.questions),
                "error": s.error[:50]
            }
            for s in self.sessions.values()
        ]

    def close_session(self, session_id: str):
        """Close debug session"""
        if session_id in self.sessions:
            self.sessions[session_id].status = "abandoned"
            logger.log_structured("INFO", "debug_interactive",
                                 f"Session closed: {session_id}")


def get_interactive_debugger() -> InteractiveDebugger:
    """Get InteractiveDebugger instance"""
    return InteractiveDebugger()

