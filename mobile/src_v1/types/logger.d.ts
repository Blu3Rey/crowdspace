/**
 * Logger — shared types
 *
 * All log entries are fully structured so sinks can filter, format, persist,
 * and ship them without needing to parse strings.
 */

// ─── Level ────────────────────────────────────────────────────────────────────

export type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'fatal'

/** Numeric priority for fast level comparison (higher = more severe). */
export const LOG_LEVEL_PRIORITY: Record<LogLevel, number> = {
  debug: 0,
  info:  1,
  warn:  2,
  error: 3,
  fatal: 4,
}

// ─── Entry ────────────────────────────────────────────────────────────────────

export interface LogEntry {
  /** Millisecond timestamp (Date.now()). */
  ts: number
  /** Severity level. */
  level: LogLevel
  /** Dot-namespaced module identifier, e.g. 'mesh:ble', 'mesh:transport'. */
  ns: string
  /** Human-readable message — concise, imperative ("Connected", not "We connected"). */
  msg: string
  /** Structured context — must be JSON-serialisable. */
  ctx?: Record<string, unknown>
  /** Error detail when an exception is attached. */
  err?: { message: string; stack?: string; code?: string }
  /** Elapsed milliseconds for log.time() spans. */
  durationMs?: number
}

// ─── Sink ─────────────────────────────────────────────────────────────────────

export interface LogSink {
  /** Receive a log entry. Never throw — the dispatcher will swallow errors. */
  write(entry: LogEntry): void
  /** Flush any buffered writes (call on app teardown). */
  flush?(): Promise<void>
}

// ─── Config ───────────────────────────────────────────────────────────────────

export interface LoggerConfig {
  /** Global minimum level. Entries below this are dropped before reaching any sink. */
  minLevel: LogLevel
  /** Sinks to write matching entries to. */
  sinks: LogSink[]
  /**
   * Per-namespace minimum level overrides.
   * Matches exact namespace names and prefix matches separated by ':'.
   * E.g. { 'mesh:ble': 'info' } suppresses debug entries from BLEEngine only.
   */
  namespaceFilters?: Record<string, LogLevel>
  /**
   * Context keys whose values will be partially redacted before reaching sinks.
   * Useful for stripping message text in production without losing structure.
   */
  redactedKeys?: string[]
}