/* Shared types for the filter-controls library.
 *
 * Trimmed-down option shape that the widgets accept regardless of
 * whether they were sourced from the canonical registry (Czech +
 * English labels) or built ad-hoc for a one-off form.
 */

export interface EnumOptionLite<T extends string | number = string> {
  value: T;
  label: string;
}
