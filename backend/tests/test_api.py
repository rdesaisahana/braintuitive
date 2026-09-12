"""Phase 4 API tests.

Two properties matter more than the rest and are tested first:

* **The answer key never reaches the student before they answer.** If it does,
  the mastery signal the whole progression rests on is worthless.
* **One parent cannot reach another family's child.** Every quiz and progress
  route is addressed by ``student_id``.

Quiz creation is stubbed onto a pre-seeded question bank, so nothing here
costs an API call.

Run:
    cd backend
    pytest tests/test_api.py -v
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.deps import get_current_user  # noqa: F401  (imported for clarity)
from db.database import get_db
from db.models import (
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    QuestionBankItem,
    QuizStatus,
    SubUnitProgress,
)
from main import app
from services.question_bank import content_hash

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    """A TestClient wired to the in-memory database."""

    def _override() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def curriculum(db: Session) -> dict[str, Any]:
    """Unit 1 with two sub-units, unit 2 locked behind it, bank stocked."""
    unit1 = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    unit2 = CurriculumUnit(unit_number=2, title="Expressions", subject="math", grade_level=6)
    sub11 = CurriculumSubUnit(
        unit=unit1, sub_unit_number="1.1", sequence=0, title="Absolute value", is_indexed=True
    )
    sub12 = CurriculumSubUnit(
        unit=unit1, sub_unit_number="1.2", sequence=1, title="Add integers", is_indexed=True
    )
    sub21 = CurriculumSubUnit(
        unit=unit2, sub_unit_number="2.1", sequence=0, title="Expressions", is_indexed=True
    )
    db.add_all([unit1, unit2, sub11, sub12, sub21])
    db.commit()

    for sub in (sub11, sub12, sub21):
        for level in DifficultyLevel:
            for index in range(12):
                text = f"{sub.sub_unit_number} {level.value} question {index}?"
                db.add(
                    QuestionBankItem(
                        sub_unit_id=sub.id,
                        difficulty_level=level,
                        question_text=text,
                        options=[{"key": k, "text": f"opt{k}"} for k in "ABCD"],
                        correct_answer="B",
                        explanation="Add the numbers, then check the sign of the result.",
                        distractor_rationales={
                            "A": "You ignored the sign.",
                            "C": "You added instead of subtracting.",
                            "D": "You reversed the order.",
                        },
                        hint="Use a number line.",
                        skill_tag="add_integers",
                        content_hash=content_hash(text),
                        verification_status="verified",
                    )
                )
    db.commit()
    return {"unit1": unit1, "unit2": unit2, "sub11": sub11, "sub12": sub12, "sub21": sub21}


def signup(client: TestClient, email: str = "parent@example.com") -> dict[str, str]:
    """Create an account, and give it the fixture curriculum if it is unclaimed.

    Curriculum is owned per family and there is no shared fallback, so a parent
    with no upload sees nothing at all. The ``curriculum`` fixture cannot assign
    an owner because it runs before any account exists, so the first account to
    sign up adopts it -- which is what an upload would have done.

    Later signups in the same test adopt nothing, which is exactly right for
    the cross-family tests: the second family genuinely has no curriculum.
    """
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct horse battery", "full_name": "A Parent"},
    )
    assert response.status_code == 201, response.text
    tokens = response.json()

    me = client.get("/api/v1/auth/me", headers=auth(tokens))
    assert me.status_code == 200, me.text
    claim_curriculum(client, me.json()["id"])
    return tokens


def claim_curriculum(client: TestClient, user_id: str) -> None:
    """Hand any unowned fixture curriculum to this account."""
    from db.models import CurriculumUnit

    session = next(iter(app.dependency_overrides.values()))()
    db = next(session)
    unowned = db.query(CurriculumUnit).filter_by(user_id=None).all()
    for unit in unowned:
        unit.user_id = user_id
    if unowned:
        db.commit()


def auth(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def make_student(client: TestClient, tokens: dict[str, str], name: str = "Aanya") -> str:
    response = client.post(
        "/api/v1/auth/students",
        json={"first_name": name, "grade_level": 6},
        headers=auth(tokens),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------------- #
# The answer key must not leak
# --------------------------------------------------------------------------- #


def test_started_quiz_contains_no_answers(client: TestClient, curriculum: dict[str, Any]) -> None:
    """The single most important assertion in this file."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    )
    assert response.status_code == 201, response.text
    payload = response.json()

    assert len(payload["questions"]) == 10
    blob = response.text.lower()
    for leak in ("correct_answer", "explanation", "distractor_rationale"):
        assert leak not in blob, f"{leak} leaked to the student"
    # The rationale text itself must not appear either.
    assert "you ignored the sign" not in blob
    # Nor the hint: it is fetched on demand, not shipped upfront.
    assert "hint" not in blob.replace("has_hint", "")
    assert "use a number line" not in blob

    for question in payload["questions"]:
        assert set(question) == {
            "id",
            "question_number",
            "question_text",
            "question_type",
            "options",
            "has_hint",
            "points",
            "answered",
        }
        assert question["has_hint"] is True
        assert question["question_type"] == "multiple_choice"
        assert question["answered"] is None, "an unanswered question carries no state"


def test_reading_a_quiz_back_also_hides_answers(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """A page refresh must not become a way to see the answers."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    again = client.get(f"/api/v1/quiz/{quiz['id']}", headers=auth(tokens))
    assert again.status_code == 200
    assert "correct_answer" not in again.text.lower()


def test_feedback_arrives_only_after_answering(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    question = quiz["questions"][0]

    wrong = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": question["id"], "selected_answer": "A"},
        headers=auth(tokens),
    )
    assert wrong.status_code == 200, wrong.text
    body = wrong.json()

    assert body["is_correct"] is False
    assert body["correct_answer"] == "B"
    assert body["explanation"]
    assert body["why_your_answer_was_wrong"] == "You ignored the sign."
    assert body["points_earned"] == 0
    assert body["answered_count"] == 1
    assert body["quiz_complete"] is False


def test_correct_answer_has_no_wrongness_rationale(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    body = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": quiz["questions"][0]["id"], "selected_answer": "B"},
        headers=auth(tokens),
    ).json()

    assert body["is_correct"] is True
    assert body["why_your_answer_was_wrong"] is None
    assert body["points_earned"] > 0
    # Nothing to teach when they got it right.
    assert body["explanation"] is None
    assert body["correct_answer"] is None


def test_a_question_cannot_be_answered_twice(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Otherwise a student reads the answer from the first response and resubmits."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    question_id = quiz["questions"][0]["id"]

    first = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": question_id, "selected_answer": "A"},
        headers=auth(tokens),
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": question_id, "selected_answer": "B"},
        headers=auth(tokens),
    )
    assert second.status_code == 409


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_a_parent_cannot_reach_another_familys_child(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    mine = signup(client, "mine@example.com")
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs, "Someone Else")

    response = client.get(f"/api/v1/auth/students/{their_student}", headers=auth(mine))
    # 404 rather than 403, so student ids cannot be enumerated.
    assert response.status_code == 404


def test_cannot_start_a_quiz_for_another_familys_child(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    mine = signup(client, "mine@example.com")
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs, "Someone Else")

    response = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": their_student,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(mine),
    )
    assert response.status_code == 404


def test_cannot_read_another_familys_quiz(client: TestClient, curriculum: dict[str, Any]) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": their_student,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(theirs),
    ).json()

    mine = signup(client, "mine@example.com")
    assert client.get(f"/api/v1/quiz/{quiz['id']}", headers=auth(mine)).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/auth/students"),
        ("post", "/api/v1/quiz/start"),
    ],
)
def test_routes_require_authentication(client: TestClient, method: str, path: str) -> None:
    # TestClient.get() takes no json body, so only send one for POST.
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


def test_a_refresh_token_is_not_accepted_as_an_access_token(client: TestClient) -> None:
    """Otherwise a leaked refresh token grants 30 days instead of 60 minutes."""
    tokens = signup(client)
    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Auth flow
# --------------------------------------------------------------------------- #


def test_signup_login_and_me(client: TestClient) -> None:
    tokens = signup(client)
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "parent@example.com", "password": "correct horse battery"},
    )
    assert login.status_code == 200

    me = client.get("/api/v1/auth/me", headers=auth(login.json()))
    assert me.status_code == 200
    assert me.json()["email"] == "parent@example.com"
    assert "hashed_password" not in me.text


def test_duplicate_signup_is_rejected(client: TestClient) -> None:
    signup(client)
    again = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "parent@example.com",
            "password": "another password",
            "full_name": "Impostor",
        },
    )
    assert again.status_code == 409


def test_wrong_password_is_rejected(client: TestClient) -> None:
    signup(client)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "parent@example.com", "password": "not the password"},
    )
    assert response.status_code == 401
    assert "password" in response.json()["detail"].lower()


def test_short_password_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": "x@example.com", "password": "short", "full_name": "X"},
    )
    assert response.status_code == 422


def test_refresh_rotates_and_revokes_the_old_token(client: TestClient) -> None:
    """A replayed refresh token must fail, which is how theft surfaces."""
    tokens = signup(client)

    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]

    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401


def test_logout_revokes_the_session(client: TestClient) -> None:
    tokens = signup(client)
    assert (
        client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 204
    )
    after = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert after.status_code == 401


def test_logout_with_an_unknown_token_still_returns_204(client: TestClient) -> None:
    """Probing which refresh tokens are live must not be possible."""
    response = client.post("/api/v1/auth/logout", json={"refresh_token": "not-a-token"})
    assert response.status_code == 204


def test_new_student_gets_a_gamification_profile(client: TestClient, db: Session) -> None:
    from db.models import GamificationProfile

    tokens = signup(client)
    student_id = make_student(client, tokens)
    assert db.query(GamificationProfile).filter_by(student_id=student_id).count() == 1


# --------------------------------------------------------------------------- #
# Full quiz flow
# --------------------------------------------------------------------------- #


def _answer_all(
    client: TestClient, tokens: dict[str, str], quiz: dict[str, Any], correct: int
) -> None:
    """Answer every question, getting ``correct`` of them right."""
    for index, question in enumerate(quiz["questions"]):
        client.post(
            f"/api/v1/quiz/{quiz['id']}/answer",
            json={
                "question_id": question["id"],
                "selected_answer": "B" if index < correct else "A",
                "time_spent_seconds": 20,
            },
            headers=auth(tokens),
        )


def test_passing_a_quiz_moves_progress(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    _answer_all(client, tokens, quiz, correct=9)
    result = client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))
    assert result.status_code == 200, result.text
    body = result.json()

    assert body["score_percentage"] == 90.0
    assert body["is_passed"] is True
    assert body["difficulty_completed"] is True
    assert body["completion_percentage"] == 33
    assert body["next_difficulty"] == "intermediate"
    assert body["should_celebrate"] is False


def test_failing_a_quiz_does_not_advance(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    _answer_all(client, tokens, quiz, correct=5)  # 50%, below the 70% bar
    body = client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).json()

    assert body["is_passed"] is False
    assert body["completion_percentage"] == 0
    assert body["next_difficulty"] == "beginner"


def test_cannot_complete_with_questions_outstanding(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    _answer_all(client, tokens, quiz, correct=0)
    # Undo one answer by starting fresh: answer only nine of ten.
    partial = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub11"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    for question in partial["questions"][:9]:
        client.post(
            f"/api/v1/quiz/{partial['id']}/answer",
            json={"question_id": question["id"], "selected_answer": "B"},
            headers=auth(tokens),
        )

    response = client.post(f"/api/v1/quiz/{partial['id']}/complete", headers=auth(tokens))
    assert response.status_code == 409
    assert "unanswered" in response.json()["detail"]


def test_completing_a_quiz_twice_is_rejected(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    _answer_all(client, tokens, quiz, correct=10)

    assert (
        client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).status_code == 200
    )
    assert (
        client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).status_code == 409
    )


def test_locked_unit_is_refused(client: TestClient, curriculum: dict[str, Any]) -> None:
    """The API must not become a way around sequential progression."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub21"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    )
    assert response.status_code == 409
    assert "locked" in response.json()["detail"].lower()


def test_celebration_fires_once_at_100_percent(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    results = []
    for level in ("beginner", "intermediate", "proficient"):
        quiz = client.post(
            "/api/v1/quiz/start",
            json={
                "student_id": student_id,
                "sub_unit_id": curriculum["sub12"].id,
                "difficulty": level,
            },
            headers=auth(tokens),
        ).json()
        _answer_all(client, tokens, quiz, correct=10)
        results.append(
            client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).json()
        )

    assert [r["completion_percentage"] for r in results] == [33, 67, 100]
    assert [r["should_celebrate"] for r in results] == [False, False, True]

    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student_id, sub_unit_id=curriculum["sub12"].id)
        .one()
    )
    # Reported but not spent: the client acknowledges it after the animation.
    assert progress.celebration_shown is False
    assert progress.should_celebrate is True


def test_invalid_option_key_is_rejected(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    response = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": quiz["questions"][0]["id"], "selected_answer": "Z"},
        headers=auth(tokens),
    )
    assert response.status_code == 422


def test_quiz_status_moves_to_in_progress(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    from db.models import Quiz

    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    assert quiz["status"] == "in_progress"
    assert db.get(Quiz, quiz["id"]).status is QuizStatus.IN_PROGRESS


# --------------------------------------------------------------------------- #
# Hints are pulled, not pushed
# --------------------------------------------------------------------------- #


def _start(client: TestClient, tokens: dict[str, str], sub_unit_id: str) -> dict[str, Any]:
    student_id = make_student(client, tokens)
    return client.post(
        "/api/v1/quiz/start",
        json={"student_id": student_id, "sub_unit_id": sub_unit_id, "difficulty": "beginner"},
        headers=auth(tokens),
    ).json()


def test_hint_endpoint_returns_the_hint(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    quiz = _start(client, tokens, curriculum["sub12"].id)
    question = quiz["questions"][0]

    response = client.post(
        f"/api/v1/quiz/{quiz['id']}/questions/{question['id']}/hint", headers=auth(tokens)
    )
    assert response.status_code == 200, response.text
    assert response.json()["hint"] == "Use a number line."


def test_requesting_a_hint_is_recorded_server_side(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    """hint_used must not be something the client can misreport."""
    from db.models import QuizQuestion, QuizResponse

    tokens = signup(client)
    quiz = _start(client, tokens, curriculum["sub12"].id)
    question_id = quiz["questions"][0]["id"]

    assert db.get(QuizQuestion, question_id).hint_requested is False

    client.post(f"/api/v1/quiz/{quiz['id']}/questions/{question_id}/hint", headers=auth(tokens))
    assert db.get(QuizQuestion, question_id).hint_requested is True

    feedback = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": question_id, "selected_answer": "B"},
        headers=auth(tokens),
    ).json()
    assert feedback["hint_used"] is True

    response = db.query(QuizResponse).filter_by(question_id=question_id).one()
    assert response.hint_used is True


def test_not_asking_for_a_hint_records_false(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    from db.models import QuizResponse

    tokens = signup(client)
    quiz = _start(client, tokens, curriculum["sub12"].id)
    question_id = quiz["questions"][0]["id"]

    feedback = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": question_id, "selected_answer": "B"},
        headers=auth(tokens),
    ).json()

    assert feedback["hint_used"] is False
    assert db.query(QuizResponse).filter_by(question_id=question_id).one().hint_used is False


def test_hint_cannot_be_fetched_after_answering(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    quiz = _start(client, tokens, curriculum["sub12"].id)
    question_id = quiz["questions"][0]["id"]

    client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": question_id, "selected_answer": "B"},
        headers=auth(tokens),
    )
    response = client.post(
        f"/api/v1/quiz/{quiz['id']}/questions/{question_id}/hint", headers=auth(tokens)
    )
    assert response.status_code == 409


def test_hint_requires_ownership(client: TestClient, curriculum: dict[str, Any]) -> None:
    theirs = signup(client, "theirs@example.com")
    quiz = _start(client, theirs, curriculum["sub12"].id)

    mine = signup(client, "mine@example.com")
    response = client.post(
        f"/api/v1/quiz/{quiz['id']}/questions/{quiz['questions'][0]['id']}/hint",
        headers=auth(mine),
    )
    assert response.status_code == 404


def test_hint_rejects_a_question_from_another_quiz(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    first = _start(client, tokens, curriculum["sub11"].id)
    second = _start(client, tokens, curriculum["sub12"].id)

    response = client.post(
        f"/api/v1/quiz/{first['id']}/questions/{second['questions'][0]['id']}/hint",
        headers=auth(tokens),
    )
    assert response.status_code == 404


def test_wrong_answer_still_teaches(client: TestClient, curriculum: dict[str, Any]) -> None:
    """The whole point of the change: feedback concentrates on mistakes."""
    tokens = signup(client)
    quiz = _start(client, tokens, curriculum["sub12"].id)

    body = client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": quiz["questions"][0]["id"], "selected_answer": "A"},
        headers=auth(tokens),
    ).json()

    assert body["is_correct"] is False
    assert body["correct_answer"] == "B"
    assert body["explanation"]
    assert body["why_your_answer_was_wrong"] == "You ignored the sign."


# --------------------------------------------------------------------------- #
# Curriculum map
# --------------------------------------------------------------------------- #


def test_curriculum_map_shape(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.get(f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens))
    assert response.status_code == 200, response.text
    units = response.json()

    assert [u["unit_number"] for u in units] == [1, 2]
    unit1, unit2 = units

    assert unit1["unlocked"] is True
    assert unit1["lock_reason"] is None
    assert len(unit1["sub_units"]) == 2
    assert unit1["completion_percentage"] == 0

    assert unit2["unlocked"] is False
    assert "locked" in unit2["lock_reason"].lower()
    assert unit2["blocking_sub_units"] == ["1.1", "1.2"]


def test_locked_units_still_list_their_sub_units(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Seeing what is coming is motivating; hiding it shrinks the course."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    units = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).json()
    locked = next(u for u in units if not u["unlocked"])
    assert len(locked["sub_units"]) == 1
    assert locked["sub_units"][0]["sub_unit_number"] == "2.1"


def test_curriculum_map_never_leaks_answers(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    blob = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).text.lower()
    for leak in ("correct_answer", "explanation", "question_text"):
        assert leak not in blob


def test_progress_appears_in_the_map(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    _answer_all(client, tokens, quiz, correct=10)
    client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))

    units = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).json()
    sub12 = next(s for s in units[0]["sub_units"] if s["sub_unit_number"] == "1.2")

    assert sub12["completion_percentage"] == 33
    assert sub12["beginner_completed"] is True
    assert sub12["next_difficulty"] == "intermediate"
    assert units[0]["completion_percentage"] == 17  # (33 + 0) / 2


def test_ready_difficulties_reflect_the_bank(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    """A tier without bank stock falls back to ~2 minutes of generation."""
    from db.models import QuestionBankItem

    tokens = signup(client)
    student_id = make_student(client, tokens)

    units = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).json()
    sub11 = next(s for s in units[0]["sub_units"] if s["sub_unit_number"] == "1.1")
    assert set(sub11["ready_difficulties"]) == {"beginner", "intermediate", "proficient"}

    # Starve the proficient slot below a full quiz.
    items = (
        db.query(QuestionBankItem)
        .filter_by(sub_unit_id=curriculum["sub11"].id, difficulty_level=DifficultyLevel.PROFICIENT)
        .all()
    )
    for item in items[:5]:
        item.is_active = False
    db.commit()

    units = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).json()
    sub11 = next(s for s in units[0]["sub_units"] if s["sub_unit_number"] == "1.1")
    assert "proficient" not in sub11["ready_difficulties"]
    assert "beginner" in sub11["ready_difficulties"]


def test_map_query_count_is_flat(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    """Regression guard: check_unit_unlocked costs 3 queries per unit.

    Calling it in a loop turns one dashboard render into 20+ round trips and
    gets worse as the curriculum grows.
    """
    from sqlalchemy import event as sa_event

    from api.routes.curriculum import build_curriculum_map
    from db.models import Student as StudentModel

    tokens = signup(client)
    student_id = make_student(client, tokens)
    student = db.get(StudentModel, student_id)

    counter = {"n": 0}

    def _count(*_args: Any, **_kwargs: Any) -> None:
        counter["n"] += 1

    sa_event.listen(db.get_bind(), "before_cursor_execute", _count)
    try:
        build_curriculum_map(db, student)
    finally:
        sa_event.remove(db.get_bind(), "before_cursor_execute", _count)

    assert counter["n"] <= 6, f"expected a handful of queries, issued {counter['n']}"


def test_read_one_unit(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    ok = client.get(f"/api/v1/curriculum/students/{student_id}/units/1", headers=auth(tokens))
    assert ok.status_code == 200
    assert ok.json()["unit_number"] == 1

    missing = client.get(f"/api/v1/curriculum/students/{student_id}/units/99", headers=auth(tokens))
    assert missing.status_code == 404


def test_curriculum_requires_ownership(client: TestClient, curriculum: dict[str, Any]) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mine@example.com")

    response = client.get(f"/api/v1/curriculum/students/{their_student}/units", headers=auth(mine))
    assert response.status_code == 404


def test_empty_curriculum_for_an_unloaded_grade(client: TestClient, db: Session) -> None:
    tokens = signup(client)
    response = client.post(
        "/api/v1/auth/students",
        json={"first_name": "Older", "grade_level": 11},
        headers=auth(tokens),
    )
    student_id = response.json()["id"]

    units = client.get(f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens))
    assert units.status_code == 200
    assert units.json() == []

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()
    assert nxt["has_next"] is False
    assert "grade 11" in nxt["message"]


# --------------------------------------------------------------------------- #
# Next action
# --------------------------------------------------------------------------- #


def test_next_action_starts_at_the_beginning(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()

    assert nxt["has_next"] is True
    assert nxt["unit_number"] == 1
    assert nxt["sub_unit_number"] == "1.1"
    assert nxt["difficulty"] == "beginner"
    assert nxt["quiz_ready"] is True


def test_next_action_advances_the_tier(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub11"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    _answer_all(client, tokens, quiz, correct=10)
    client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()
    assert nxt["sub_unit_number"] == "1.1"
    assert nxt["difficulty"] == "intermediate"


def test_next_action_never_points_into_a_locked_unit(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()
    assert nxt["unit_number"] == 1


# --------------------------------------------------------------------------- #
# Celebration acknowledgement
# --------------------------------------------------------------------------- #


def _complete_sub_unit(
    client: TestClient, tokens: dict[str, str], student_id: str, sub_unit_id: str
) -> dict[str, Any]:
    """Take a sub-unit to 100% and return the final quiz result."""
    result: dict[str, Any] = {}
    for level in ("beginner", "intermediate", "proficient"):
        quiz = client.post(
            "/api/v1/quiz/start",
            json={"student_id": student_id, "sub_unit_id": sub_unit_id, "difficulty": level},
            headers=auth(tokens),
        ).json()
        _answer_all(client, tokens, quiz, correct=10)
        result = client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).json()
    return result


def test_celebration_survives_until_acknowledged(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """A lost response or a closed tab must not cost a child their 100%."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    result = _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)
    assert result["should_celebrate"] is True

    # The client never acknowledged; the dashboard still offers it.
    units = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).json()
    sub12 = next(s for s in units[0]["sub_units"] if s["sub_unit_number"] == "1.2")
    assert sub12["should_celebrate"] is True
    assert sub12["completion_percentage"] == 100


def test_acknowledging_spends_the_celebration(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)

    ack = client.post(
        f"/api/v1/curriculum/students/{student_id}/celebrations/{curriculum['sub12'].id}/ack",
        headers=auth(tokens),
    )
    assert ack.status_code == 204

    units = client.get(
        f"/api/v1/curriculum/students/{student_id}/units", headers=auth(tokens)
    ).json()
    sub12 = next(s for s in units[0]["sub_units"] if s["sub_unit_number"] == "1.2")
    assert sub12["should_celebrate"] is False
    assert sub12["completion_percentage"] == 100, "acknowledging must not undo progress"


def test_acknowledging_twice_is_harmless(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)

    url = f"/api/v1/curriculum/students/{student_id}/celebrations/{curriculum['sub12'].id}/ack"
    assert client.post(url, headers=auth(tokens)).status_code == 204
    assert client.post(url, headers=auth(tokens)).status_code == 204


def test_acknowledging_an_untouched_sub_unit_is_404(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.post(
        f"/api/v1/curriculum/students/{student_id}/celebrations/{curriculum['sub11'].id}/ack",
        headers=auth(tokens),
    )
    assert response.status_code == 404


def test_celebration_ack_requires_ownership(client: TestClient, curriculum: dict[str, Any]) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    _complete_sub_unit(client, theirs, their_student, curriculum["sub12"].id)

    mine = signup(client, "mine@example.com")
    response = client.post(
        f"/api/v1/curriculum/students/{their_student}/celebrations/{curriculum['sub12'].id}/ack",
        headers=auth(mine),
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Progress: the parent dashboard
# --------------------------------------------------------------------------- #


def _take_quiz(
    client: TestClient,
    tokens: dict[str, str],
    student_id: str,
    sub_unit_id: str,
    difficulty: str,
    correct: int,
) -> dict[str, Any]:
    quiz = client.post(
        "/api/v1/quiz/start",
        json={"student_id": student_id, "sub_unit_id": sub_unit_id, "difficulty": difficulty},
        headers=auth(tokens),
    ).json()
    _answer_all(client, tokens, quiz, correct=correct)
    return client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).json()


def test_summary_for_a_fresh_student(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["student_name"] == "Aanya"
    assert body["grade_level"] == 6
    assert body["sub_units_total"] == 3
    assert body["sub_units_completed"] == 0
    assert body["units_completed"] == 0
    assert body["current_unit"] == 1
    assert body["overall_percentage"] == 0.0
    assert body["quizzes_completed"] == 0
    assert body["current_streak_days"] == 0
    assert body["last_active"] is None
    assert len(body["sub_units"]) == 3


def test_summary_reflects_work_done(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=9)

    body = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()

    assert body["quizzes_completed"] == 1
    assert body["average_score"] == 90.0
    assert body["sub_units_completed"] == 0  # 33%, not finished
    assert body["overall_percentage"] == 11.0  # 33 / 3 sub-units
    assert body["current_streak_days"] == 1
    assert body["last_active"] is not None

    sub12 = next(s for s in body["sub_units"] if s["sub_unit_number"] == "1.2")
    assert sub12["beginner_best_score"] == 90.0
    assert sub12["completion_percentage"] == 33
    assert sub12["total_attempts"] == 1


def test_practice_is_excluded_from_statistics_by_default(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """A low-stakes retry must not drag down the average a parent reads."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    # Master the tier, then a dismal practice run.
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=1)

    default = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()
    assert default["quizzes_completed"] == 1
    assert default["practice_quizzes"] == 1
    assert default["average_score"] == 100.0

    with_practice = client.get(
        f"/api/v1/progress/students/{student_id}?include_practice=true",
        headers=auth(tokens),
    ).json()
    assert with_practice["average_score"] == 55.0  # (100 + 10) / 2


def test_units_completed_requires_every_sub_unit(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub11"].id)

    body = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()
    assert body["sub_units_completed"] == 1
    assert body["units_completed"] == 0, "unit 1 still has 1.2 outstanding"
    assert body["current_unit"] == 1

    _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)
    body = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()
    assert body["units_completed"] == 1
    assert body["current_unit"] == 2


def test_celebrations_pending_is_surfaced(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)

    body = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()
    assert body["celebrations_pending"] == 1

    client.post(
        f"/api/v1/curriculum/students/{student_id}/celebrations/{curriculum['sub12'].id}/ack",
        headers=auth(tokens),
    )
    body = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()
    assert body["celebrations_pending"] == 0


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #


def test_skill_stats_aggregate_answers(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=6)

    skills = client.get(
        f"/api/v1/progress/students/{student_id}/skills", headers=auth(tokens)
    ).json()

    assert len(skills) == 1
    stat = skills[0]
    assert stat["skill_tag"] == "add_integers"
    assert stat["questions_answered"] == 10
    assert stat["correct"] == 6
    assert stat["accuracy"] == 0.6
    assert stat["needs_attention"] is True  # 60% over 10 answers


def test_strong_skill_is_not_flagged(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=9)

    stat = client.get(
        f"/api/v1/progress/students/{student_id}/skills", headers=auth(tokens)
    ).json()[0]
    assert stat["accuracy"] == 0.9
    assert stat["needs_attention"] is False


def test_hint_usage_counts_towards_the_skill(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Right-but-needed-a-hint is a different signal from right."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    for index, question in enumerate(quiz["questions"]):
        if index < 3:
            client.post(
                f"/api/v1/quiz/{quiz['id']}/questions/{question['id']}/hint",
                headers=auth(tokens),
            )
        client.post(
            f"/api/v1/quiz/{quiz['id']}/answer",
            json={"question_id": question["id"], "selected_answer": "B"},
            headers=auth(tokens),
        )
    client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))

    stat = client.get(
        f"/api/v1/progress/students/{student_id}/skills", headers=auth(tokens)
    ).json()[0]
    assert stat["accuracy"] == 1.0
    assert stat["hints_used"] == 3


def test_skills_are_weakest_first(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    from db.models import QuestionBankItem

    # Give sub-unit 1.1 a different skill tag so two skills exist.
    for item in db.query(QuestionBankItem).filter_by(sub_unit_id=curriculum["sub11"].id).all():
        item.skill_tag = "absolute_value"
    db.commit()

    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub11"].id, "beginner", correct=9)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=3)

    skills = client.get(
        f"/api/v1/progress/students/{student_id}/skills", headers=auth(tokens)
    ).json()
    assert [s["skill_tag"] for s in skills] == ["add_integers", "absolute_value"]
    assert skills[0]["needs_attention"] is True
    assert skills[1]["needs_attention"] is False


def test_skills_ignore_practice_by_default(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=0)

    default = client.get(
        f"/api/v1/progress/students/{student_id}/skills", headers=auth(tokens)
    ).json()[0]
    assert default["accuracy"] == 1.0
    assert default["questions_answered"] == 10

    included = client.get(
        f"/api/v1/progress/students/{student_id}/skills?include_practice=true",
        headers=auth(tokens),
    ).json()[0]
    assert included["questions_answered"] == 20
    assert included["accuracy"] == 0.5


# --------------------------------------------------------------------------- #
# Attempt history
# --------------------------------------------------------------------------- #


def test_attempt_history_is_newest_first(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub11"].id, "beginner", correct=8)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=9)

    attempts = client.get(
        f"/api/v1/progress/students/{student_id}/attempts", headers=auth(tokens)
    ).json()

    assert len(attempts) == 2
    assert attempts[0]["sub_unit_number"] == "1.2"
    assert attempts[0]["score_percentage"] == 90.0
    assert attempts[0]["unit_number"] == 1
    assert attempts[0]["is_practice"] is False


def test_practice_shows_in_history_but_is_labelled(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """A parent should see the work even where it does not count."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=2)

    shown = client.get(
        f"/api/v1/progress/students/{student_id}/attempts", headers=auth(tokens)
    ).json()
    assert len(shown) == 2
    assert shown[0]["is_practice"] is True

    graded = client.get(
        f"/api/v1/progress/students/{student_id}/attempts?include_practice=false",
        headers=auth(tokens),
    ).json()
    assert len(graded) == 1
    assert graded[0]["is_practice"] is False


def test_attempt_history_paginates(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    for _ in range(3):
        _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=5)

    page = client.get(
        f"/api/v1/progress/students/{student_id}/attempts?limit=2", headers=auth(tokens)
    ).json()
    assert len(page) == 2

    rest = client.get(
        f"/api/v1/progress/students/{student_id}/attempts?limit=2&offset=2",
        headers=auth(tokens),
    ).json()
    assert len(rest) == 1


# --------------------------------------------------------------------------- #
# Sub-unit detail
# --------------------------------------------------------------------------- #


def test_sub_unit_detail(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=8)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "intermediate", correct=9)

    body = client.get(
        f"/api/v1/progress/students/{student_id}/sub-units/{curriculum['sub12'].id}",
        headers=auth(tokens),
    ).json()

    assert body["sub_unit_number"] == "1.2"
    assert body["unit_number"] == 1
    assert body["completion_percentage"] == 67
    assert body["beginner_best_score"] == 80.0
    assert body["intermediate_best_score"] == 90.0
    assert body["next_difficulty"] == "proficient"
    assert len(body["attempts"]) == 2
    assert body["skills"][0]["questions_answered"] == 20


def test_sub_unit_detail_for_untouched_sub_unit(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    body = client.get(
        f"/api/v1/progress/students/{student_id}/sub-units/{curriculum['sub11'].id}",
        headers=auth(tokens),
    ).json()
    assert body["completion_percentage"] == 0
    assert body["status"] == "not_started"
    assert body["attempts"] == []
    assert body["next_difficulty"] is None


def test_sub_unit_detail_unknown_id(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    response = client.get(
        f"/api/v1/progress/students/{student_id}/sub-units/nope", headers=auth(tokens)
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Ownership and streaks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", ["", "/skills", "/attempts"])
def test_progress_requires_ownership(
    client: TestClient, curriculum: dict[str, Any], suffix: str
) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mine@example.com")

    response = client.get(f"/api/v1/progress/students/{their_student}{suffix}", headers=auth(mine))
    assert response.status_code == 404


def test_streak_counts_back_from_today() -> None:
    from datetime import date as _date

    from api.routes.progress import _streak_days

    today = _date(2026, 9, 9)
    assert _streak_days(set(), today) == 0
    assert _streak_days({today}, today) == 1
    assert _streak_days({today, _date(2026, 9, 8), _date(2026, 9, 7)}, today) == 3
    # A gap ends the streak.
    assert _streak_days({today, _date(2026, 9, 7)}, today) == 1


def test_streak_survives_a_day_not_yet_started() -> None:
    """Worked last night, has not started today -- the streak still stands."""
    from datetime import date as _date

    from api.routes.progress import _streak_days

    today = _date(2026, 9, 9)
    yesterday = _date(2026, 9, 8)
    assert _streak_days({yesterday, _date(2026, 9, 7)}, today) == 2
    # But two days idle does break it.
    assert _streak_days({_date(2026, 9, 7)}, today) == 0


# --------------------------------------------------------------------------- #
# Resuming an abandoned quiz
# --------------------------------------------------------------------------- #


def _start_and_answer(
    client: TestClient,
    tokens: dict[str, str],
    student_id: str,
    sub_unit_id: str,
    answer_count: int,
    difficulty: str = "beginner",
) -> dict[str, Any]:
    """Start a quiz and answer the first ``answer_count`` questions."""
    quiz = client.post(
        "/api/v1/quiz/start",
        json={"student_id": student_id, "sub_unit_id": sub_unit_id, "difficulty": difficulty},
        headers=auth(tokens),
    ).json()
    for question in quiz["questions"][:answer_count]:
        client.post(
            f"/api/v1/quiz/{quiz['id']}/answer",
            json={"question_id": question["id"], "selected_answer": "B"},
            headers=auth(tokens),
        )
    return quiz


def test_starting_again_resumes_the_same_quiz(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """The core requirement: come back tomorrow, land on the same quiz."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    first = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 4)

    again = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    assert again["id"] == first["id"], "a second quiz was created instead of resuming"
    assert again["resumed"] is True
    assert again["answered_count"] == 4
    assert again["next_question_number"] == 5
    assert again["all_answered"] is False
    # Same questions, in the same order.
    assert [q["id"] for q in again["questions"]] == [q["id"] for q in first["questions"]]


def test_resume_does_not_burn_more_bank_questions(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    from db.models import Quiz as QuizModel

    tokens = signup(client)
    student_id = make_student(client, tokens)
    _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 3)

    for _ in range(3):
        client.post(
            "/api/v1/quiz/start",
            json={
                "student_id": student_id,
                "sub_unit_id": curriculum["sub12"].id,
                "difficulty": "beginner",
            },
            headers=auth(tokens),
        )

    assert db.query(QuizModel).filter_by(student_id=student_id).count() == 1


def test_resumed_quiz_shows_previous_answers(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """The child should see what they already did, not a blank slate."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    # One right, one wrong.
    client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": quiz["questions"][0]["id"], "selected_answer": "B"},
        headers=auth(tokens),
    )
    client.post(
        f"/api/v1/quiz/{quiz['id']}/answer",
        json={"question_id": quiz["questions"][1]["id"], "selected_answer": "A"},
        headers=auth(tokens),
    )

    resumed = client.get(f"/api/v1/quiz/{quiz['id']}", headers=auth(tokens)).json()
    right, wrong, untouched = (
        resumed["questions"][0],
        resumed["questions"][1],
        resumed["questions"][2],
    )

    assert right["answered"]["is_correct"] is True
    assert right["answered"]["selected_answer"] == "B"
    # A correct answer still gets no explanation.
    assert right["answered"]["explanation"] is None
    assert right["answered"]["correct_answer"] is None

    assert wrong["answered"]["is_correct"] is False
    assert wrong["answered"]["correct_answer"] == "B"
    assert wrong["answered"]["explanation"]
    assert wrong["answered"]["why_your_answer_was_wrong"] == "You ignored the sign."

    assert untouched["answered"] is None


def test_resumed_quiz_still_hides_unanswered_answers(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Resuming must not become a way to read ahead."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 2)

    resumed = client.get(f"/api/v1/quiz/{quiz['id']}", headers=auth(tokens)).json()
    for question in resumed["questions"][2:]:
        assert question["answered"] is None


def test_there_is_no_way_to_discard_an_unfinished_quiz(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    """Resuming is unconditional -- repeated starts never mint a second quiz."""
    from db.models import Quiz as QuizModel

    tokens = signup(client)
    student_id = make_student(client, tokens)
    first = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 5)

    body = {
        "student_id": student_id,
        "sub_unit_id": curriculum["sub12"].id,
        "difficulty": "beginner",
    }
    # Including the query flag that used to force a fresh quiz: it is gone, and
    # FastAPI ignores unknown query parameters, so this must still resume.
    again = client.post("/api/v1/quiz/start?force_new=true", json=body, headers=auth(tokens)).json()

    assert again["id"] == first["id"]
    assert again["resumed"] is True
    assert again["answered_count"] == 5
    assert db.query(QuizModel).filter_by(student_id=student_id).count() == 1


def test_resume_is_scoped_to_the_same_tier(client: TestClient, curriculum: dict[str, Any]) -> None:
    """An unfinished beginner quiz must not be handed back for intermediate."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    beginner = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 2)

    intermediate = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "intermediate",
        },
        headers=auth(tokens),
    ).json()

    assert intermediate["id"] != beginner["id"]
    assert intermediate["resumed"] is False


def test_completed_quiz_is_not_resumed(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    done = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    _answer_all(client, tokens, done, correct=10)
    client.post(f"/api/v1/quiz/{done['id']}/complete", headers=auth(tokens))

    nxt = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()
    assert nxt["id"] != done["id"]
    assert nxt["resumed"] is False


def test_all_answered_but_unsubmitted_still_resumes(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Answered everything then closed the tab: they must get back to submit."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 10)

    again = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    assert again["id"] == quiz["id"]
    assert again["all_answered"] is True
    assert again["next_question_number"] is None


# --------------------------------------------------------------------------- #
# Next action points at the resume
# --------------------------------------------------------------------------- #


def test_next_action_prefers_an_unfinished_quiz(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Log in the next day and be sent straight back to where you stopped."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 6)

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()

    assert nxt["action"] == "resume"
    assert nxt["resume_quiz_id"] == quiz["id"]
    assert nxt["answered_count"] == 6
    assert nxt["next_question_number"] == 7
    assert nxt["sub_unit_number"] == "1.2"
    assert nxt["unit_number"] == 1
    assert nxt["difficulty"] == "beginner"
    assert "left off" in nxt["message"]


def test_next_action_resumes_even_from_a_later_sub_unit(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """The unfinished quiz wins over the earliest incomplete sub-unit."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    # 1.1 is untouched and would normally be "next"; 1.2 is half done.
    _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 3)

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()
    assert nxt["action"] == "resume"
    assert nxt["sub_unit_number"] == "1.2"


def test_next_action_starts_fresh_when_nothing_is_open(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()
    assert nxt["action"] == "start"
    assert nxt["resume_quiz_id"] is None
    assert nxt["sub_unit_number"] == "1.1"


def test_next_action_returns_to_start_after_completing(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = _start_and_answer(client, tokens, student_id, curriculum["sub12"].id, 10)
    client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))

    nxt = client.get(f"/api/v1/curriculum/students/{student_id}/next", headers=auth(tokens)).json()
    assert nxt["action"] == "start"
    assert nxt["resume_quiz_id"] is None


# --------------------------------------------------------------------------- #
# Gamification: points, levels, streaks, badges
# --------------------------------------------------------------------------- #


def _profile(client: TestClient, tokens: dict[str, str], student_id: str) -> dict[str, Any]:
    return client.get(f"/api/v1/gamification/students/{student_id}", headers=auth(tokens)).json()


def test_new_student_starts_at_level_one(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)

    body = _profile(client, tokens, student_id)
    assert body["total_points"] == 0
    assert body["points_spent"] == 0
    assert body["points_balance"] == 0
    assert body["level"] == 1
    assert body["current_streak_days"] == 0
    assert body["badges_earned"] == 0
    assert body["badges_total"] > 0

    # Every starter, free, from the moment the profile exists -- so nobody
    # begins as a default nobody chose.
    from services.avatars import starter_keys

    assert set(body["unlocked_avatars"]) == set(starter_keys())
    assert body["avatar_key"] in starter_keys()


def test_completing_a_quiz_awards_points(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    result = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=8)

    award = result["award"]
    assert award is not None
    assert award["points_earned"] > 0
    assert award["breakdown"]["correct_answers"] == 80  # 8 x 10
    assert award["breakdown"]["passed"] == 25
    assert award["current_streak_days"] == 1
    assert award["streak_extended"] is True

    body = _profile(client, tokens, student_id)
    assert body["total_points"] == award["total_points"]
    assert body["total_quizzes_completed"] == 1
    assert body["total_correct_answers"] == 8


def test_practice_earns_nothing(client: TestClient, curriculum: dict[str, Any]) -> None:
    """Otherwise the whole economy is farmable by replaying one easy quiz."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    before = _profile(client, tokens, student_id)["total_points"]

    practice = _take_quiz(
        client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10
    )
    assert practice["is_practice"] is True
    assert practice["award"]["points_earned"] == 0
    assert practice["award"]["new_badges"] == []

    after = _profile(client, tokens, student_id)
    assert after["total_points"] == before
    assert after["total_quizzes_completed"] == 1, "practice must not inflate counters"


def test_failing_still_pays_for_correct_answers(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Effort counts even when the bar is missed."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    result = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=4)

    award = result["award"]
    assert result["is_passed"] is False
    assert award["breakdown"]["correct_answers"] == 40
    assert "passed" not in award["breakdown"]


def test_perfect_score_pays_a_bonus(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    result = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)

    breakdown = result["award"]["breakdown"]
    assert breakdown["correct_answers"] == 100
    assert breakdown["passed"] == 25
    assert breakdown["perfect_score"] == 25
    assert breakdown["tier_complete"] == 50


def test_tier_bonus_is_paid_only_once(client: TestClient, curriculum: dict[str, Any]) -> None:
    """Re-passing the same tier must not pay the completion bonus again."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    first = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    assert "tier_complete" in first["award"]["breakdown"]

    # A repeat is practice, which earns nothing at all.
    second = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    assert "tier_complete" not in second["award"]["breakdown"]


def test_levelling_up_is_reported(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    result = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)

    assert result["award"]["levelled_up"] is True
    assert result["award"]["level"] >= 2


def test_completing_a_sub_unit_pays_and_badges(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    final = _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)

    assert final["completion_percentage"] == 100
    assert final["award"]["breakdown"].get("sub_unit_complete") == 100
    keys = {b["badge_key"] for b in final["award"]["new_badges"]}
    assert "sub_unit_master" in keys

    body = _profile(client, tokens, student_id)
    assert body["total_sub_units_completed"] == 1


def test_completing_a_unit_pays_and_badges(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub11"].id)
    final = _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)

    assert final["award"]["breakdown"].get("unit_complete") == 250
    keys = {b["badge_key"] for b in final["award"]["new_badges"]}
    assert "unit_master" in keys

    body = _profile(client, tokens, student_id)
    assert body["total_units_completed"] == 1


# --------------------------------------------------------------------------- #
# Badges
# --------------------------------------------------------------------------- #


def test_first_quiz_and_first_pass_badges(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    result = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=8)

    keys = {b["badge_key"] for b in result["award"]["new_badges"]}
    assert {"first_quiz", "first_pass"} <= keys


def test_a_badge_is_never_awarded_twice(
    client: TestClient, curriculum: dict[str, Any], db: Session
) -> None:
    """A celebration that repeats is not a celebration."""
    from db.models import AchievementBadge

    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=10)
    second = _take_quiz(client, tokens, student_id, curriculum["sub11"].id, "beginner", correct=10)

    keys = {b["badge_key"] for b in second["award"]["new_badges"]}
    assert "first_quiz" not in keys
    assert "perfect_score" not in keys

    counts = db.query(AchievementBadge).filter_by(student_id=student_id).all()
    assert len({b.badge_key for b in counts}) == len(counts), "duplicate badge rows"


def test_unaided_perfect_requires_no_hints(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    quiz = client.post(
        "/api/v1/quiz/start",
        json={
            "student_id": student_id,
            "sub_unit_id": curriculum["sub12"].id,
            "difficulty": "beginner",
        },
        headers=auth(tokens),
    ).json()

    # Perfect score, but a hint was used on one question.
    client.post(
        f"/api/v1/quiz/{quiz['id']}/questions/{quiz['questions'][0]['id']}/hint",
        headers=auth(tokens),
    )
    _answer_all(client, tokens, quiz, correct=10)
    result = client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens)).json()

    keys = {b["badge_key"] for b in result["award"]["new_badges"]}
    assert "perfect_score" in keys
    assert "unaided_perfect" not in keys, "a hint was used"


def test_comeback_badge_after_a_failure(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=3)
    result = _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=9)

    keys = {b["badge_key"] for b in result["award"]["new_badges"]}
    assert "comeback" in keys


def test_badge_list_shows_locked_ones_too(client: TestClient, curriculum: dict[str, Any]) -> None:
    """A child needs to see what is still available to win."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=8)

    everything = client.get(
        f"/api/v1/gamification/students/{student_id}/badges", headers=auth(tokens)
    ).json()
    assert len(everything) > 2
    assert any(b["earned"] for b in everything)
    assert any(not b["earned"] for b in everything)
    # Earned badges sort first.
    assert everything[0]["earned"] is True

    only_earned = client.get(
        f"/api/v1/gamification/students/{student_id}/badges?earned_only=true",
        headers=auth(tokens),
    ).json()
    assert all(b["earned"] for b in only_earned)
    assert len(only_earned) < len(everything)


# --------------------------------------------------------------------------- #
# Streaks
# --------------------------------------------------------------------------- #


def test_streak_logic() -> None:
    from datetime import date as _date

    from db.models import GamificationProfile
    from services.gamification import update_streak

    today = _date(2026, 9, 9)
    profile = GamificationProfile(student_id="x")

    assert update_streak(profile, today) is True
    assert profile.current_streak_days == 1

    # Twice in one day does not count twice.
    assert update_streak(profile, today) is False
    assert profile.current_streak_days == 1

    assert update_streak(profile, _date(2026, 9, 10)) is True
    assert profile.current_streak_days == 2

    # A missed day restarts it, but the record stands.
    assert update_streak(profile, _date(2026, 9, 13)) is True
    assert profile.current_streak_days == 1
    assert profile.longest_streak_days == 2


# --------------------------------------------------------------------------- #
# Avatars
# --------------------------------------------------------------------------- #


def test_avatar_must_be_unlocked(client: TestClient, curriculum: dict[str, Any]) -> None:
    """An avatar you can set by asking is not a reward."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.post(
        f"/api/v1/gamification/students/{student_id}/avatar",
        json={"avatar_key": "ziggy"},
        headers=auth(tokens),
    )
    assert response.status_code == 403
    assert "not unlocked" in response.json()["detail"]


def test_an_avatar_is_bought_with_points(client: TestClient, curriculum: dict[str, Any]) -> None:
    """Earning is the whole loop: answer questions, save up, choose someone new.

    Buying deducts from the balance but never from ``total_points``, because
    that is what drives the level -- dropping a child's level for using their
    reward would be the opposite of a reward.
    """
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)

    before = _profile(client, tokens, student_id)
    assert before["points_balance"] >= 300, before["points_balance"]

    bought = client.post(
        f"/api/v1/gamification/students/{student_id}/avatars/nova/buy",
        headers=auth(tokens),
    )
    assert bought.status_code == 200, bought.text

    after = bought.json()
    assert "nova" in after["unlocked_avatars"]
    assert after["avatar_key"] == "nova", "buying it should wear it"
    assert after["points_spent"] == 300
    assert after["points_balance"] == before["points_balance"] - 300
    assert after["total_points"] == before["total_points"], "lifetime points must not fall"
    assert after["level"] == before["level"], "spending must not cost a level"


def test_an_avatar_you_cannot_afford_is_refused(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """And the message is written for the child, not the log."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.post(
        f"/api/v1/gamification/students/{student_id}/avatars/ziggy/buy",
        headers=auth(tokens),
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "1000" in detail and "more to go" in detail, detail


def test_the_shop_lists_what_cannot_be_afforded_yet(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """A shop that hides what you cannot afford gives a child nothing to save
    towards."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    shop = client.get(
        f"/api/v1/gamification/students/{student_id}/avatars", headers=auth(tokens)
    ).json()

    keys = {item["key"] for item in shop["avatars"]}
    assert "ziggy" in keys
    dragon = next(item for item in shop["avatars"] if item["key"] == "ziggy")
    assert dragon["owned"] is False
    assert dragon["affordable"] is False
    assert dragon["price"] == 1000
    assert shop["points_balance"] == 0


def test_wearing_an_unowned_avatar_is_refused(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """An avatar that can be set by asking is not a reward."""
    tokens = signup(client)
    student_id = make_student(client, tokens)

    response = client.post(
        f"/api/v1/gamification/students/{student_id}/avatar",
        json={"avatar_key": "ziggy"},
        headers=auth(tokens),
    )
    assert response.status_code == 403


def test_level_progress_is_a_fraction(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=6)

    body = _profile(client, tokens, student_id)
    assert 0.0 <= body["level_progress"] <= 1.0


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", ["", "/badges"])
def test_gamification_requires_ownership(
    client: TestClient, curriculum: dict[str, Any], suffix: str
) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mine@example.com")

    response = client.get(
        f"/api/v1/gamification/students/{their_student}{suffix}", headers=auth(mine)
    )
    assert response.status_code == 404


def test_cannot_set_another_familys_avatar(client: TestClient, curriculum: dict[str, Any]) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mine@example.com")

    response = client.post(
        f"/api/v1/gamification/students/{their_student}/avatar",
        json={"avatar_key": "sunny"},
        headers=auth(mine),
    )
    assert response.status_code == 404


def test_no_gamification_endpoint_grants_points(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Points must only ever come from completing a quiz.

    This guard caught the avatar shop being added, which is what it is for.
    The shop is allowed because it only moves points *out*; the assertion is
    now that every gamification POST leaves the total no higher than it was.
    """
    from main import app

    posts = sorted(
        route.path
        for route in app.routes
        if hasattr(route, "methods")
        and "POST" in route.methods
        and "gamification" in getattr(route, "path", "")
    )
    assert posts == [
        "/api/v1/gamification/students/{student_id}/avatar",
        "/api/v1/gamification/students/{student_id}/avatars/{avatar_key}/buy",
    ], posts

    tokens = signup(client)
    student_id = make_student(client, tokens)
    _complete_sub_unit(client, tokens, student_id, curriculum["sub12"].id)
    before = _profile(client, tokens, student_id)["total_points"]

    client.post(
        f"/api/v1/gamification/students/{student_id}/avatars/nova/buy",
        headers=auth(tokens),
    )
    client.post(
        f"/api/v1/gamification/students/{student_id}/avatar",
        json={"avatar_key": "nova"},
        headers=auth(tokens),
    )

    after = _profile(client, tokens, student_id)["total_points"]
    assert after <= before, "a gamification POST increased lifetime points"


# --------------------------------------------------------------------------- #
# Gap analysis
# --------------------------------------------------------------------------- #


def test_gap_analysis_is_mounted_and_authenticated(client: TestClient) -> None:
    """A router that fails to mount is silent; only a request proves it is there.

    This project has already shipped a router that mounted as a no-op, so the
    endpoint existing is worth asserting directly rather than inferring.
    """
    tokens = signup(client, "gaps@example.com")
    student_id = make_student(client, tokens)

    assert client.post(f"/api/v1/progress/students/{student_id}/gaps").status_code == 401

    response = client.post(f"/api/v1/progress/students/{student_id}/gaps", headers=auth(tokens))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["student_id"] == student_id
    # No answers yet: the honest reply is "not enough data", not an invented gap.
    assert body["gaps"] == []
    assert body["responses_analysed"] == 0
    assert body["summary"]


def test_cannot_analyse_another_familys_child(client: TestClient) -> None:
    theirs = signup(client, "theirgaps@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mygaps@example.com")

    response = client.post(f"/api/v1/progress/students/{their_student}/gaps", headers=auth(mine))
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Study planning
# --------------------------------------------------------------------------- #


def test_study_plan_is_mounted_and_authenticated(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client, "prep@example.com")
    student_id = make_student(client, tokens)
    body = {"unit_numbers": [1], "days_until_test": 4, "question_budget": 20}

    assert (
        client.post(f"/api/v1/progress/students/{student_id}/study-plan", json=body).status_code
        == 401
    )

    response = client.post(
        f"/api/v1/progress/students/{student_id}/study-plan",
        json=body,
        headers=auth(tokens),
    )
    assert response.status_code == 200, response.text

    plan = response.json()
    assert plan["student_id"] == student_id
    assert plan["days_until_test"] == 4
    assert plan["total_questions"] <= 20
    # Nothing attempted yet, so every topic in scope is an unstarted one.
    assert plan["topics_not_yet_started"]


def test_study_plan_rejects_an_absurd_timeframe(client: TestClient) -> None:
    """Validation belongs at the edge; the agent clamps, the API refuses."""
    tokens = signup(client, "prepbad@example.com")
    student_id = make_student(client, tokens)

    response = client.post(
        f"/api/v1/progress/students/{student_id}/study-plan",
        json={"days_until_test": 900},
        headers=auth(tokens),
    )
    assert response.status_code == 422


def test_cannot_plan_for_another_familys_child(client: TestClient) -> None:
    theirs = signup(client, "theirprep@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "myprep@example.com")

    response = client.post(
        f"/api/v1/progress/students/{their_student}/study-plan",
        json={"days_until_test": 3},
        headers=auth(mine),
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Cumulative unit tests
# --------------------------------------------------------------------------- #


def sit_unit_test(
    client: TestClient, tokens: dict[str, str], student_id: str, unit_id: str, count: int = 8
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/quiz/unit-test",
        json={"student_id": student_id, "unit_id": unit_id, "question_count": count},
        headers=auth(tokens),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_unit_test_interleaves_and_hides_the_answers(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Both invariants at once: the paper alternates sub-units, and no
    question carries its answer to the client."""
    tokens = signup(client, "ut@example.com")
    student_id = make_student(client, tokens)

    quiz = sit_unit_test(client, tokens, student_id, curriculum["unit1"].id)

    assert quiz["is_unit_test"] is True
    assert quiz["sub_unit_id"] is None
    assert quiz["unit_number"] == 1

    for question in quiz["questions"]:
        assert "correct_answer" not in question
        assert "explanation" not in question


def test_an_unfinished_unit_test_is_handed_back(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """Same promise as any other quiz: return to the paper, not to its start."""
    tokens = signup(client, "utresume@example.com")
    student_id = make_student(client, tokens)

    first = sit_unit_test(client, tokens, student_id, curriculum["unit1"].id)
    second = sit_unit_test(client, tokens, student_id, curriculum["unit1"].id)

    assert second["id"] == first["id"]
    assert second["resumed"] is True


def test_a_unit_test_scores_by_sub_unit_and_moves_no_progress(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client, "utscore@example.com")
    student_id = make_student(client, tokens)
    quiz = sit_unit_test(client, tokens, student_id, curriculum["unit1"].id)

    for question in quiz["questions"]:
        answered = client.post(
            f"/api/v1/quiz/{quiz['id']}/answer",
            json={"question_id": question["id"], "selected_answer": "B"},
            headers=auth(tokens),
        )
        assert answered.status_code == 200, answered.text

    result = client.post(f"/api/v1/quiz/{quiz['id']}/complete-unit-test", headers=auth(tokens))
    assert result.status_code == 200, result.text

    body = result.json()
    assert body["unit_number"] == 1
    assert body["score_percentage"] == 100.0
    assert {score["sub_unit_number"] for score in body["sub_unit_scores"]} == {"1.1", "1.2"}

    # Nothing about a cumulative paper may unlock the next unit.
    summary = client.get(f"/api/v1/progress/students/{student_id}", headers=auth(tokens)).json()
    assert summary["overall_percentage"] == 0
    assert summary["sub_units_completed"] == 0


def test_the_ordinary_complete_endpoint_rejects_a_unit_test(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    """The two results differ in shape; silently using the wrong one would
    report a completion_percentage that means nothing."""
    tokens = signup(client, "utwrong@example.com")
    student_id = make_student(client, tokens)
    quiz = sit_unit_test(client, tokens, student_id, curriculum["unit1"].id)

    response = client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))
    assert response.status_code in (400, 409), response.text


def test_a_locked_unit_cannot_be_tested(client: TestClient, curriculum: dict[str, Any]) -> None:
    tokens = signup(client, "utlocked@example.com")
    student_id = make_student(client, tokens)

    response = client.post(
        "/api/v1/quiz/unit-test",
        json={"student_id": student_id, "unit_id": curriculum["unit2"].id},
        headers=auth(tokens),
    )
    assert response.status_code == 409, response.text


def test_cannot_start_a_unit_test_for_another_familys_child(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    theirs = signup(client, "theirut@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "myut@example.com")

    response = client.post(
        "/api/v1/quiz/unit-test",
        json={"student_id": their_student, "unit_id": curriculum["unit1"].id},
        headers=auth(mine),
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Curriculum upload
# --------------------------------------------------------------------------- #

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


def test_a_new_account_has_no_curriculum_at_all(client: TestClient) -> None:
    """What the app asks before deciding to show the upload screen.

    No fixture curriculum here on purpose: this is a genuinely fresh account,
    and the honest answer is that it has nothing until a parent uploads.
    """
    tokens = signup(client, "upl@example.com")

    body = client.get("/api/v1/curriculum/status", headers=auth(tokens)).json()
    assert body["has_own_curriculum"] is False
    assert body["units"] == 0
    assert body["active_upload"] is None


def test_an_account_with_a_curriculum_says_so(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client, "owned@example.com")

    body = client.get("/api/v1/curriculum/status", headers=auth(tokens)).json()
    assert body["has_own_curriculum"] is True
    assert body["units"] > 0


def test_uploading_a_curriculum_is_accepted_and_queued(
    client: TestClient, curriculum: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """202, not 201: the file is accepted but the curriculum does not exist
    yet -- parsing, embedding and indexing take minutes."""
    queued: list[str] = []
    monkeypatch.setattr(
        "api.routes.curriculum.run_preview", lambda upload_id: queued.append(upload_id)
    )
    tokens = signup(client, "upl2@example.com")

    response = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("guide.pdf", PDF_BYTES, "application/pdf")},
        data={"subject": "math", "grade_level": "6"},
        headers=auth(tokens),
    )
    assert response.status_code == 202, response.text

    body = response.json()
    assert body["status"] == "pending"
    assert body["filename"] == "guide.pdf"
    assert queued == [body["id"]], "the read was not queued"

    polled = client.get(f"/api/v1/curriculum/uploads/{body['id']}", headers=auth(tokens))
    assert polled.status_code == 200
    assert polled.json()["id"] == body["id"]


def test_a_renamed_non_pdf_is_refused(client: TestClient) -> None:
    """Checked by magic bytes, not the name or the content type the client
    chose to send."""
    tokens = signup(client, "upl3@example.com")

    response = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("guide.pdf", b"PK\x03\x04 not a pdf", "application/pdf")},
        headers=auth(tokens),
    )
    assert response.status_code == 422
    assert "PDF" in response.json()["detail"]


def test_a_second_upload_is_refused_while_one_is_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent ingests would interleave two curricula into one."""
    monkeypatch.setattr("api.routes.curriculum.run_preview", lambda upload_id: None)
    tokens = signup(client, "upl4@example.com")
    files = {"file": ("guide.pdf", PDF_BYTES, "application/pdf")}

    first = client.post("/api/v1/curriculum/upload", files=files, headers=auth(tokens))
    assert first.status_code == 202

    second = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("other.pdf", PDF_BYTES, "application/pdf")},
        headers=auth(tokens),
    )
    assert second.status_code == 409
    assert "still being processed" in second.json()["detail"]


def test_upload_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("guide.pdf", PDF_BYTES, "application/pdf")},
    )
    assert response.status_code == 401


def test_cannot_poll_another_accounts_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.routes.curriculum.run_preview", lambda upload_id: None)
    theirs = signup(client, "theirupl@example.com")
    created = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("guide.pdf", PDF_BYTES, "application/pdf")},
        headers=auth(theirs),
    ).json()

    mine = signup(client, "myupl@example.com")
    response = client.get(f"/api/v1/curriculum/uploads/{created['id']}", headers=auth(mine))
    assert response.status_code == 404, "another account's upload must not be readable"


# --------------------------------------------------------------------------- #
# Confirming an upload before it is built
# --------------------------------------------------------------------------- #

PREVIEW = {
    "title": "Grade 6 Pre-Algebra",
    "grade_level": 6,
    "page_count": 40,
    "total_units": 2,
    "total_topics": 5,
    "units": [
        {"unit_number": 1, "title": "Number Fluency", "topics": 3},
        {"unit_number": 2, "title": "Expressions", "topics": 2},
    ],
}


def upload_awaiting_review(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, email: str
) -> tuple[dict[str, str], str]:
    """Upload a PDF and leave it where the read leaves it: understood, unbuilt."""
    from db.models import CurriculumUpload, UploadStatus

    monkeypatch.setattr("api.routes.curriculum.run_preview", lambda upload_id: None)
    tokens = signup(client, email)
    created = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("guide.pdf", PDF_BYTES, "application/pdf")},
        headers=auth(tokens),
    ).json()

    row = db.get(CurriculumUpload, created["id"])
    row.status = UploadStatus.REVIEW
    row.preview = PREVIEW
    db.commit()
    return tokens, created["id"]


def test_an_upload_is_read_but_not_built(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploading queues only the cheap read. Embedding and question writing --
    the expensive part -- wait for the parent to say it is the right file."""
    read: list[str] = []
    built: list[str] = []
    monkeypatch.setattr(
        "api.routes.curriculum.run_preview", lambda upload_id: read.append(upload_id)
    )
    monkeypatch.setattr(
        "api.routes.curriculum.run_ingestion", lambda upload_id: built.append(upload_id)
    )
    tokens = signup(client, "readonly@example.com")

    body = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("guide.pdf", PDF_BYTES, "application/pdf")},
        headers=auth(tokens),
    ).json()

    assert read == [body["id"]]
    assert built == [], "nothing may be built before the parent confirms"


def test_the_preview_is_shown_for_review(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens, upload_id = upload_awaiting_review(client, db, monkeypatch, "rev1@example.com")

    polled = client.get(f"/api/v1/curriculum/uploads/{upload_id}", headers=auth(tokens)).json()
    assert polled["status"] == "review"
    assert polled["preview"]["grade_level"] == 6
    assert [unit["title"] for unit in polled["preview"]["units"]] == [
        "Number Fluency",
        "Expressions",
    ]


def test_saying_yes_builds_it(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens, upload_id = upload_awaiting_review(client, db, monkeypatch, "rev2@example.com")
    built: list[str] = []
    monkeypatch.setattr(
        "api.routes.curriculum.run_ingestion", lambda upload_id: built.append(upload_id)
    )

    response = client.post(f"/api/v1/curriculum/uploads/{upload_id}/confirm", headers=auth(tokens))
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    assert built == [upload_id]


def test_saying_no_builds_nothing(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens, upload_id = upload_awaiting_review(client, db, monkeypatch, "rev3@example.com")
    built: list[str] = []
    monkeypatch.setattr(
        "api.routes.curriculum.run_ingestion", lambda upload_id: built.append(upload_id)
    )

    response = client.post(f"/api/v1/curriculum/uploads/{upload_id}/cancel", headers=auth(tokens))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert built == []


def test_confirming_twice_builds_once(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A double-click on "Yes" must not queue the build twice."""
    tokens, upload_id = upload_awaiting_review(client, db, monkeypatch, "rev4@example.com")
    built: list[str] = []
    monkeypatch.setattr(
        "api.routes.curriculum.run_ingestion", lambda upload_id: built.append(upload_id)
    )

    first = client.post(f"/api/v1/curriculum/uploads/{upload_id}/confirm", headers=auth(tokens))
    second = client.post(f"/api/v1/curriculum/uploads/{upload_id}/confirm", headers=auth(tokens))

    assert first.status_code == 202
    assert second.status_code == 409
    assert built == [upload_id]


def test_a_waiting_upload_blocks_another_and_says_why(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens, _ = upload_awaiting_review(client, db, monkeypatch, "rev5@example.com")

    second = client.post(
        "/api/v1/curriculum/upload",
        files={"file": ("other.pdf", PDF_BYTES, "application/pdf")},
        headers=auth(tokens),
    )
    assert second.status_code == 409
    assert "confirm" in second.json()["detail"]


def test_the_question_comes_back_after_a_reload(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walking away mid-decision must not lose it: the status the page loads
    on arrival carries the upload still waiting to be confirmed."""
    tokens, upload_id = upload_awaiting_review(client, db, monkeypatch, "rev6@example.com")

    status_body = client.get("/api/v1/curriculum/status", headers=auth(tokens)).json()
    assert status_body["active_upload"]["id"] == upload_id
    assert status_body["active_upload"]["status"] == "review"
    assert status_body["active_upload"]["preview"]["total_units"] == 2


def test_cannot_decide_on_another_accounts_upload(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, upload_id = upload_awaiting_review(client, db, monkeypatch, "theirrev@example.com")
    mine = signup(client, "myrev@example.com")

    assert (
        client.post(f"/api/v1/curriculum/uploads/{upload_id}/confirm", headers=auth(mine))
    ).status_code == 404
    assert (
        client.post(f"/api/v1/curriculum/uploads/{upload_id}/cancel", headers=auth(mine))
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Removing a child
# --------------------------------------------------------------------------- #


def test_a_parent_can_remove_a_child(client: TestClient, curriculum: dict[str, Any]) -> None:
    """Gone means gone: the child, and the work that was theirs."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=6)

    response = client.delete(f"/api/v1/auth/students/{student_id}", headers=auth(tokens))
    assert response.status_code == 204, response.text

    assert client.get(f"/api/v1/auth/students/{student_id}", headers=auth(tokens)).status_code == 404
    listed = client.get("/api/v1/auth/students", headers=auth(tokens)).json()
    assert all(item["id"] != student_id for item in listed)
    history = client.get(f"/api/v1/progress/students/{student_id}/attempts", headers=auth(tokens))
    assert history.status_code == 404


def test_another_familys_child_cannot_be_removed(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mine@example.com")

    response = client.delete(f"/api/v1/auth/students/{their_student}", headers=auth(mine))
    assert response.status_code == 404
    still_there = client.get(f"/api/v1/auth/students/{their_student}", headers=auth(theirs))
    assert still_there.status_code == 200


# --------------------------------------------------------------------------- #
# Revision review for a topic
# --------------------------------------------------------------------------- #


def _start_beginner_quiz(client: TestClient, tokens: dict[str, str], student_id: str, sub_unit_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/quiz/start",
        json={"student_id": student_id, "sub_unit_id": sub_unit_id, "difficulty": "beginner"},
        headers=auth(tokens),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_review_lists_wrong_and_hinted_questions(client: TestClient, curriculum: dict[str, Any]) -> None:
    """The wrong answers with their feedback, the hinted ones with the hint."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    sub_unit_id = curriculum["sub12"].id
    quiz = _start_beginner_quiz(client, tokens, student_id, sub_unit_id)

    hinted_id = quiz["questions"][0]["id"]
    hint = client.post(f"/api/v1/quiz/{quiz['id']}/questions/{hinted_id}/hint", headers=auth(tokens))
    assert hint.status_code == 200, hint.text
    _answer_all(client, tokens, quiz, correct=6)  # the first six right, the last four wrong
    client.post(f"/api/v1/quiz/{quiz['id']}/complete", headers=auth(tokens))

    response = client.get(
        f"/api/v1/progress/students/{student_id}/sub-units/{sub_unit_id}/review", headers=auth(tokens)
    )
    assert response.status_code == 200, response.text
    review = response.json()

    assert review["questions_answered"] == 10
    assert len(review["missed"]) == 4
    for question in review["missed"]:
        assert question["selected_answer"] == "A"
        assert question["correct_answer"] == "B"
        assert question["explanation"], "a wrong answer carries the feedback it was shown"
        assert question["is_correct"] is False

    assert [q["question_id"] for q in review["hinted"]] == [hinted_id]
    hinted = review["hinted"][0]
    assert hinted["hint"], "the hint that was asked for comes back"
    assert hinted["explanation"] is None, "a right answer keeps its explanation to itself"
    assert hinted["correct_answer"] is None

    assert review["revise"], "a topic with mistakes has something to revise"
    assert review["revise_level"] == "beginner"
    assert "Easy" in review["summary"]


def test_review_of_a_clean_topic_says_nothing_to_revise(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    tokens = signup(client)
    student_id = make_student(client, tokens)
    sub_unit_id = curriculum["sub12"].id
    _take_quiz(client, tokens, student_id, sub_unit_id, "beginner", correct=10)

    review = client.get(
        f"/api/v1/progress/students/{student_id}/sub-units/{sub_unit_id}/review", headers=auth(tokens)
    ).json()

    assert review["missed"] == [] and review["hinted"] == [] and review["revise"] == []
    assert review["revise_level"] is None
    assert review["summary"].startswith("Nothing to revise")


def test_review_of_another_familys_child_is_not_reachable(
    client: TestClient, curriculum: dict[str, Any]
) -> None:
    theirs = signup(client, "theirs@example.com")
    their_student = make_student(client, theirs)
    mine = signup(client, "mine@example.com")

    response = client.get(
        f"/api/v1/progress/students/{their_student}/sub-units/{curriculum['sub12'].id}/review",
        headers=auth(mine),
    )
    assert response.status_code == 404


def test_attempts_name_their_topic(client: TestClient, curriculum: dict[str, Any]) -> None:
    """The history links to a topic's review, so it has to carry the topic's id."""
    tokens = signup(client)
    student_id = make_student(client, tokens)
    _take_quiz(client, tokens, student_id, curriculum["sub12"].id, "beginner", correct=7)

    attempts = client.get(f"/api/v1/progress/students/{student_id}/attempts", headers=auth(tokens)).json()
    assert attempts[0]["sub_unit_id"] == curriculum["sub12"].id
