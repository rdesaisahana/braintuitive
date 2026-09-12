/**
 * Types mirroring the backend's OpenAPI schema.
 *
 * Hand-written rather than generated so the shapes that carry a rule can carry
 * the rule's reasoning too. The important one is `AnsweredState`: the server
 * omits `correct_answer` and `explanation` entirely until a question has been
 * answered *wrongly*, so those fields are optional here for a reason, not out
 * of caution. If they ever arrive on an unanswered question, that is a leak.
 */

export type DifficultyLevel = 'beginner' | 'intermediate' | 'proficient'
export type ProgressStatus = 'not_started' | 'in_progress' | 'completed'
export type QuizStatus = 'pending' | 'in_progress' | 'completed' | 'abandoned'

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface User {
  id: string
  email: string
  full_name: string
  role: string
  is_active: boolean
  is_verified: boolean
  created_at: string
}

export interface Student {
  id: string
  first_name: string
  last_name: string | null
  grade_level: number
  is_active: boolean
  created_at: string
}

/** Where to put the child when they open the app. */
export interface NextAction {
  has_next: boolean
  action: 'resume' | 'start' | 'none'
  resume_quiz_id: string | null
  answered_count: number
  next_question_number: number | null
  unit_id: string | null
  unit_number: number | null
  unit_title: string | null
  sub_unit_id: string | null
  sub_unit_number: string | null
  sub_unit_title: string | null
  difficulty: DifficultyLevel | null
  quiz_ready: boolean
  message: string
}

export interface SubUnit {
  id: string
  sub_unit_number: string
  title: string
  description: string | null
  sequence: number
  completion_percentage: number
  status: ProgressStatus
  beginner_completed: boolean
  intermediate_completed: boolean
  proficient_completed: boolean
  next_difficulty: DifficultyLevel | null
  should_celebrate: boolean
  ready_difficulties: DifficultyLevel[]
}

export interface Unit {
  id: string
  unit_number: number
  title: string
  description: string | null
  total_sub_units: number
  unlocked: boolean
  lock_reason: string | null
  blocking_sub_units: string[]
  completion_percentage: number
  sub_units: SubUnit[]
}

export interface Option {
  key: string
  text: string
}

/**
 * What the student was already shown for a question they have answered.
 *
 * `correct_answer` and `explanation` are present only when the answer was
 * wrong -- a deliberate product rule, not an oversight: telling a child why
 * they were right when they already were is noise.
 */
export interface AnsweredState {
  selected_answer: string | null
  is_correct: boolean
  hint_used: boolean
  correct_answer?: string | null
  explanation?: string | null
  why_your_answer_was_wrong?: string | null
}

export interface Question {
  id: string
  question_number: number
  question_text: string
  /** 'true_false' questions have two options: A "True" and B "False". */
  question_type?: 'multiple_choice' | 'true_false'
  options: Option[]
  has_hint: boolean
  points: number
  /** Null while unanswered. Carries no answer fields at all in that state. */
  answered: AnsweredState | null
}

export interface Quiz {
  id: string
  student_id: string
  sub_unit_id: string | null
  sub_unit_number: string
  unit_id: string | null
  unit_number: number | null
  unit_title: string | null
  difficulty_level: DifficultyLevel
  status: QuizStatus
  total_questions: number
  passing_threshold: number
  is_practice: boolean
  is_unit_test: boolean
  resumed: boolean
  answered_count: number
  next_question_number: number | null
  all_answered: boolean
  questions: Question[]
}

export interface AnswerFeedback {
  question_id: string
  is_correct: boolean
  correct_answer?: string | null
  explanation?: string | null
  why_your_answer_was_wrong?: string | null
  hint_used: boolean
  points_earned: number
  answered_count: number
  total_questions: number
  quiz_complete: boolean
}

export interface Badge {
  badge_key: string
  badge_name: string
  description: string | null
  icon: string | null
  tier: string
  points_awarded: number
  earned: boolean
  unlocked_at: string | null
}

export interface Award {
  points_earned: number
  total_points: number
  level: number
  levelled_up: boolean
  points_to_next_level: number
  current_streak_days: number
  streak_extended: boolean
  new_badges: Badge[]
  avatars_unlocked: string[]
  breakdown: Record<string, number>
}

export interface QuizResult {
  quiz_id: string
  score_percentage: number
  correct_count: number
  total_questions: number
  passing_threshold: number
  is_passed: boolean
  is_practice: boolean
  attempt_number: number
  completion_percentage: number
  difficulty_completed: boolean
  sub_unit_complete: boolean
  /** True only when all three tiers are done -- the real milestone. */
  should_celebrate: boolean
  next_difficulty: DifficultyLevel | null
  award: Award | null
}

export interface SubUnitScore {
  sub_unit_id: string
  sub_unit_number: string
  sub_unit_title: string
  correct: number
  total: number
  accuracy: number
}

export interface UnitTestResult {
  quiz_id: string
  unit_id: string
  unit_number: number
  unit_title: string
  score_percentage: number
  correct_count: number
  total_questions: number
  passing_threshold: number
  is_passed: boolean
  attempt_number: number
  sub_unit_scores: SubUnitScore[]
  weakest_sub_units: string[]
  award: Award | null
}

export interface ProgressSummary {
  student_id: string
  student_name: string
  grade_level: number
  current_unit: number | null
  units_completed: number
  sub_units_completed: number
  sub_units_total: number
  overall_percentage: number
  quizzes_completed: number
  practice_quizzes: number
  average_score: number
  total_time_seconds: number
  current_streak_days: number
  last_active: string | null
  celebrations_pending: number
}

export interface GamificationProfile {
  student_id: string
  student_name: string
  /** Lifetime earnings. Drives the level and never falls, even after spending. */
  total_points: number
  points_spent: number
  /** What is actually left to spend in the shop. */
  points_balance: number
  level: number
  points_to_next_level: number
  level_progress: number
  current_streak_days: number
  longest_streak_days: number
  avatar_key: string
  avatar_image: string
  avatar_name: string
  unlocked_avatars: string[]
  total_quizzes_completed: number
  total_correct_answers: number
  badges_earned: number
  badges_total: number
}

/** One character in the shop, as this child sees it. */
export interface AvatarOption {
  key: string
  name: string
  image: string
  price: number
  blurb: string
  is_starter: boolean
  owned: boolean
  affordable: boolean
}

export interface AvatarShop {
  points_balance: number
  total_points: number
  points_spent: number
  wearing: string
  avatars: AvatarOption[]
}

/** One attempt at ingesting a curriculum PDF. Polled while it runs. */
/** What the parser found in an upload, shown before anything is built. */
export interface CurriculumPreview {
  title: string
  /** 0 when the cover page did not state a grade. */
  grade_level: number
  page_count: number
  total_units: number
  total_topics: number
  units: { unit_number: number; title: string; topics: number }[]
}

export interface CurriculumUpload {
  id: string
  filename: string
  subject: string
  grade_level: number | null
  /**
   * `review` means the PDF has been read and is waiting for the parent to
   * confirm it -- nothing is built until they do. `cancelled` means they
   * said it was the wrong file.
   */
  status: 'pending' | 'processing' | 'review' | 'completed' | 'failed' | 'cancelled'
  /** Written for the parent -- they are the only one who can fix the input. */
  error: string | null
  units_written: number
  sub_units_written: number
  chunks_created: number
  vectors_upserted: number
  warnings: string[]
  /** What the parser found. Present from `review` onwards. */
  preview: CurriculumPreview | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

/** Whether this family is working from their own curriculum yet. */
export interface CurriculumStatus {
  has_own_curriculum: boolean
  units: number
  using_sample: boolean
  active_upload: CurriculumUpload | null
  /** The PDF the curriculum in use came from, shown attached in the picker. */
  filename: string | null
}

/** One way to earn points, as the server describes it. */
export interface EarningRule {
  key: string
  points: number
  label: string
  detail: string
}

/**
 * How points work.
 *
 * Served rather than written into the interface, so the numbers a child reads
 * are the numbers the award engine actually pays.
 */
export interface PointsGuide {
  earning: EarningRule[]
  quiz_worth: number
  cheapest_avatar_price: number
  spend_on: string
}

/**
 * What removing a curriculum would take, or did take.
 *
 * Counted rather than described: curriculum is the root of everything, so a
 * parent clicking the cross is also deleting quizzes, progress and the
 * question bank, and should see the real numbers before agreeing.
 */
export interface CurriculumDeletion {
  units: number
  sub_units: number
  quizzes: number
  progress_rows: number
  bank_questions: number
  uploads: number
  vectors_dropped: boolean
}

/** One completed quiz, for the history list (`/progress/students/{id}/attempts`). */
export interface Attempt {
  id: string
  unit_number: number | null
  /** Links the history to that topic's revision plan. */
  sub_unit_id: string | null
  sub_unit_number: string
  sub_unit_title: string
  difficulty_level: DifficultyLevel
  attempt_number: number
  score_percentage: number
  correct_count: number
  total_questions: number
  is_passed: boolean
  is_practice: boolean
  duration_seconds: number
  completed_at: string
}

/**
 * Accuracy for one skill, worked out from individual answers rather than quiz
 * scores -- "right, but needed a hint" is a different signal from "right".
 */
export interface SkillStat {
  skill_tag: string
  questions_answered: number
  correct: number
  /** 0 to 1. */
  accuracy: number
  hints_used: number
  needs_attention: boolean
}

/** One slice of revision a child can start directly. */
export interface DrillBlock {
  sub_unit_id: string
  sub_unit_number: string
  sub_unit_title: string
  unit_number: number
  difficulty: DifficultyLevel
  question_count: number
  risk: number
  reasons: string[]
  /** False means these questions have to be written live, which takes minutes. */
  bank_ready: boolean
}

/** One evening's work in the revision plan. */
export interface StudySession {
  day: number
  focus: string
  question_count: number
  blocks: DrillBlock[]
}

/** Why a topic did, or did not, make the plan. */
export interface TopicRisk {
  sub_unit_number: string
  sub_unit_title: string
  unit_number: number
  risk: number
  never_attempted: boolean
  completion_percentage: number
  days_since_practice: number | null
  weakest_skill: string | null
  reasons: string[]
}

export interface StudyPlan {
  student_id: string
  student_name: string
  unit_numbers: number[]
  days_until_test: number
  total_questions: number
  summary: string
  advice: string[]
  sessions: StudySession[]
  risks: TopicRisk[]
  topics_not_yet_started: string[]
  warnings: string[]
}

/**
 * One question from a topic worth looking at again. Carries only what the
 * child was shown when they answered: the answer and explanation for a wrong
 * answer, the hint when they asked for one.
 */
export interface ReviewQuestion {
  question_id: string
  question_text: string
  question_type: 'multiple_choice' | 'true_false' | string
  options: Option[]
  difficulty_level: DifficultyLevel
  skill_tag: string | null
  selected_answer: string | null
  is_correct: boolean
  hint_used: boolean
  correct_answer: string | null
  explanation: string | null
  why_your_answer_was_wrong: string | null
  hint: string | null
  since_answered_correctly: boolean
  answered_at: string
}

export interface RevisionPoint {
  skill_tag: string
  skill_name: string
  answered: number
  wrong: number
  hints_used: number
  message: string
}

/** What to revise in one topic, worked out from the child's own answers. */
export interface TopicReview {
  sub_unit_id: string
  sub_unit_number: string
  title: string
  unit_number: number | null
  unit_title: string | null
  completion_percentage: number
  questions_answered: number
  summary: string
  revise_level: DifficultyLevel | null
  revise: RevisionPoint[]
  missed: ReviewQuestion[]
  hinted: ReviewQuestion[]
}
