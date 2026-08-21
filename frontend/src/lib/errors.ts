/* Errors the USER is meant to read.
 *
 * Most thrown errors are for us: a TypeError, a failed invariant, a bug. Their
 * message is diagnostic text and showing it raw to the operator is noise at
 * best and alarming at worst — the stale-chunk incident put `TypeError: Cannot
 * read properties of undefined (reading 'default')` on a full-page crash screen
 * for something that was neither the operator's fault nor data loss.
 *
 * A `UserFacingError` is the opposite: a condition we anticipated, with copy
 * written for a person and a concrete next step. `ErrorBoundary` renders those
 * two fields instead of the raw message and keeps the technical text tucked
 * away for reporting. Subclass this whenever a failure has a sentence a
 * non-technical reader would actually find useful; throw a plain Error when it
 * doesn't. */
export class UserFacingError extends Error {
  constructor(
    /* One sentence, plain language: what happened, in the user's terms. */
    readonly userMessage: string,
    /* One sentence: what to do about it. */
    readonly recovery: string,
    options?: ErrorOptions,
  ) {
    super(userMessage, options);
    this.name = 'UserFacingError';
  }
}
