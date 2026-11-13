import { ApiError } from "./client";

/**
 * The codes that have a translation of their own. The list is explicit rather than
 * "we will substitute the code into a key template": an unknown code would then
 * silently turn into a missing key, and the person would see `auth.error.teapot`
 * instead of a comprehensible message.
 */
const AUTH_CODES = new Set([
  "email_taken",
  "bad_credentials",
  "signup_disabled",
  "not_authenticated",
  "session_expired",
  "validation_error",
  "password_too_short",
  "company_name_required",
  "invalid_token",
  "token_expired",
  "already_verified",
  "too_many_requests",
]);

const PLAIN_CODES = new Set([
  "network",
  // A refusal invented by the client rather than by the server: with a dropped
  // connection a change is not sent at all. It lives in the shared list because it is
  // shown the same way as a server refusal — to a person there is no difference.
  "offline",
  "no_organization",
  "forbidden",
  "project_not_found",
  // Domain refusals. The list is explicit for the same reason as the sign-in codes':
  // substituting a code into a key template would turn an unfamiliar code into a
  // missing translation, and the person would read `task_limit_reached`.
  "category_not_found",
  "task_not_found",
  // The quote. Three refusals, as with the plan: somebody else's or a non-existent
  // entity, and an empty proposal with nothing to transfer into the plan.
  "proposal_category_not_found",
  "proposal_task_not_found",
  "proposal_empty",
  // All the selected lines are already in the plan or have no estimate: there is nothing to transfer.
  "proposal_nothing_to_push",
  // The recomputation on a unit change does not fit into the server's column.
  "proposal_value_out_of_range",
  "task_limit_reached",
  "duration_too_short",
  "user_not_in_organization",
  "calendar_has_no_working_days",
  "calendar_too_few_working_days",
  "progress_out_of_range",
  "unknown_criticality",
  // Links. Before arrows could be dragged, these refusals were almost unreachable —
  // the list in the card showed neither itself nor already linked tasks. A circle on a
  // bar's edge does not care where it is dragged, and "a cycle" became an ordinary
  // answer: a person sees two bars rather than the whole graph.
  "self_dependency",
  "dependency_exists",
  "dependency_cycle",
  "dependency_not_found",
  // Milestones and shifting a category.
  "milestone_has_duration",
  "task_is_milestone",
  "empty_shift",
  "category_empty",
  "date_out_of_range",
  "already_assigned",
  "assignment_not_found",
  "negative_position",
  "nothing_to_undo",
  // What is asked to be undone is not what lies on top of the journal: while the
  // person was reading the toast, the top moved on. The refusal reaches a person
  // rarely — the button is usually already disabled by then — but it is exactly what
  // closes the race between a glance at the cache and the server's answer.
  "undo_conflict",
  // Rolling the batch back would delete tasks added to its categories afterwards.
  "batch_has_later_tasks",
  "batch_not_found",
  "slug_taken",
  "slug_empty",
  "unsupported_locale",
  // Invitations. Expired, revoked and already accepted are three different codes and
  // three different messages: a person must understand whether to ask for a new link
  // or whether they are already in the system and simply need to sign in.
  // Public access and comments.
  "link_not_found",
  "sharing_disabled",
  "share_link_not_found",
  "comments_closed",
  "comment_empty",
  "comment_too_long",
  "guest_name_required",
  "too_many_comments",
  // Export. "The scale is too large" and "the project is too big" are named
  // separately: the first a person fixes themselves — by narrowing the period or
  // taking a coarser scale — while the second runs into the install's ceiling, and
  // there is nothing they can do about it.
  "export_empty_selection",
  "export_scale_too_wide",
  "export_period_undated",
  "export_too_large",
  "rate_limited",
  // Invitations. The three states of a dead link are named separately rather than with
  // a single "the link is invalid": on "expired" a person asks for a new one, on
  // "accepted" they simply sign in, on "revoked" they go to whoever invited them.
  "invite_not_found",
  "invite_expired",
  "invite_revoked",
  "invite_accepted",
  "invite_wrong_email",
  "invite_rate_limited",
  "invalid_email",
  "unknown_role",
  "organization_not_found",
  // Mail refusals. A separate code for each: "the email did not go out" and "mail is
  // not configured in this install" are fixed by different people.
  "mail_failed",
  "mail_not_configured",
  "mail_disabled",
  "invite_for_another_address",
  "invite_rate_limited",
  "email_not_verified",
  "role_not_invitable",
  // The organization's roster. "Already inside" is a refusal to issue an invitation:
  // there is nothing to invite an active member with, and accepting it would not
  // change their role anyway.
  "already_member",
  // The last owner is neither demoted nor allowed to leave: an organization without an
  // owner is not demoted but locked — there is nobody left in it to appoint a new one.
  "last_owner",
  "member_not_found",
  // AI. A model failure is a state a person is told about in words: the conversation
  // and the draft are preserved at that.
  "llm_not_configured",
  "llm_unreachable",
  "llm_refused",
  "llm_schema_mismatch",
  "llm_bad_json",
  "llm_bad_shape",
  "llm_key_unreadable",
  "llm_failed",
  "api_key_required",
  "wrong_step",
  "already_applied",
  "ai_session_not_found",
  "interview_exhausted",
  "nothing_asked",
  // The AI limits are two different refusals: a request limit passes in a minute while
  // an exhausted budget does not, and "try again later" on the second would read as a
  // false promise.
  "ai_rate_limited",
  "ai_budget_exhausted",
  // A project has one public link: a repeat "share" is not a breakage but an answer of
  // "there already is one".
  "share_link_exists",
  // The edit engine's refusals. They are usually never reached — the values come from
  // lists — but when the client's build is out of sync with the server, a person must
  // see words rather than "unknown error".
  "unknown_status",
  "unknown_risk",
  "unknown_operation",
  "revision_not_found",
  "plan_version_not_found",
  // This refusal usually does not reach a person: the interface asks for a reason and
  // repeats the operation. The translation is needed for the case where there is
  // nowhere to ask — for example, the dialog was closed before the server answered.
  "reason_required",
  // The scorecard. The recomputation limit is the only one of the four a person meets
  // in ordinary work: the "Recalculate" button is pressed more often than once a minute.
  "rate_limited",
  "metric_not_found",
  "week_in_future",
  "target_out_of_range",
  // Jira. The same principle as with the LLM refusals: a third-party service's failure
  // is not a breakage but a state a person is told about in words.
  "jira_not_configured",
  "jira_not_linked",
  "jira_unreachable",
  "jira_refused",
  "jira_unauthorized",
  "jira_not_found",
  "jira_bad_json",
  "jira_bad_shape",
  "jira_key_unreadable",
  "jira_token_required",
  "jira_url_not_https",
  "jira_url_invalid",
  "jira_url_private",
]);

/** The dictionary key the error is explained by. The raw code never gets out. */
export function errorKey(error: unknown): string {
  const code = error instanceof ApiError ? error.code : "unknown";
  if (AUTH_CODES.has(code)) return `auth.error.${code}`;
  if (PLAIN_CODES.has(code)) return `error.${code}`;
  return "error.unknown";
}
