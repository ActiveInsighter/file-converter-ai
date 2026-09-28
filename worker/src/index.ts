import { DurableObject } from "cloudflare:workers";

type RpcResult = {
  status: number;
  body: Record<string, unknown>;
};

type ProjectRow = {
  group_id: string;
  ordinal: number;
  requests_today: number;
  pacific_date: string;
  cooldown_until: number;
  daily_exhausted: number;
  success_count: number;
  rate_limited_count: number;
  error_count: number;
  last_key_index: number;
};

type LeaseRow = {
  lease_id: string;
  group_id: string;
  key_index: number;
  created_at: number;
  http_status: number | null;
  daily_exhausted: number;
  reported_at: number | null;
};

const pacificDate = (timestamp = Date.now()): string => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(timestamp);
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return part("year") + "-" + part("month") + "-" + part("day");
};

const asRecord = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const positiveNumber = (value: unknown): number | null => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
};

const dateEpoch = (value: unknown): number => {
  if (typeof value !== "string") return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

const randomInteger = (minimum: number, maximum: number): number => {
  const range = maximum - minimum + 1;
  const value = new Uint32Array(1);
  crypto.getRandomValues(value);
  return minimum + (value[0] % range);
};

function secureEquals(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function json(body: Record<string, unknown>, status = 200): Response {
  return Response.json(body, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

async function readJson(request: Request): Promise<unknown> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new Error("Content-Type must be application/json");
  }
  return request.json();
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const expectedToken = env.QUOTA_API_TOKEN ?? "";
    const authorization = request.headers.get("authorization") ?? "";
    const suppliedToken = authorization.startsWith("Bearer ")
      ? authorization.slice(7)
      : "";
    if (!expectedToken || !secureEquals(suppliedToken, expectedToken)) {
      return json({ error: "unauthorized" }, 401);
    }

    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);

    const poolId = env.QUOTA_POOL.idFromName("global-gemini-project-pool");
    const pool = env.QUOTA_POOL.get(poolId);
    const path = new URL(request.url).pathname;
    const routes: Record<string, (body: unknown) => Promise<RpcResult>> = {
      "/v1/configure": async (body) => pool.configure(body),
      "/v1/lease": async () => pool.acquire(),
      "/v1/report": async (body) => pool.report(body),
      "/v1/status": async () => pool.status(),
      "/v1/admin/import-state": async (body) => pool.importLegacyState(body),
    };
    const route = routes[path];
    if (!route) return json({ error: "not_found" }, 404);

    let body: unknown = {};
    try {
      body = await readJson(request);
    } catch (error) {
      return json(
        { error: error instanceof Error ? error.message : "invalid_json" },
        400,
      );
    }

    try {
      const result = await route.call(null, body);
      return json(result.body, result.status);
    } catch (error) {
      console.error("quota request failed", error);
      return json({ error: "internal_error" }, 500);
    }
  },
};

export class GeminiQuotaPool extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS projects (
          group_id TEXT PRIMARY KEY,
          ordinal INTEGER NOT NULL,
          requests_today INTEGER NOT NULL DEFAULT 0,
          pacific_date TEXT NOT NULL,
          cooldown_until INTEGER NOT NULL DEFAULT 0,
          daily_exhausted INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0,
          rate_limited_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          last_key_index INTEGER NOT NULL DEFAULT -1,
          last_assigned_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS key_map (
          key_index INTEGER PRIMARY KEY,
          group_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recent_requests (
          group_id TEXT NOT NULL,
          requested_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS recent_requests_by_project_time
          ON recent_requests(group_id, requested_at);
        CREATE INDEX IF NOT EXISTS recent_requests_by_time
          ON recent_requests(requested_at);
        CREATE TABLE IF NOT EXISTS leases (
          lease_id TEXT PRIMARY KEY,
          group_id TEXT NOT NULL,
          key_index INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          http_status INTEGER,
          daily_exhausted INTEGER NOT NULL DEFAULT 0,
          reported_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS leases_by_creation_time ON leases(created_at);
        CREATE INDEX IF NOT EXISTS leases_active_by_creation_time
          ON leases(created_at) WHERE reported_at IS NULL;
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recent_outcomes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          http_status INTEGER NOT NULL,
          reported_at INTEGER NOT NULL
        );
      `);
    });
  }

  private getMeta(key: string): string | null {
    return (
      this.ctx.storage.sql
        .exec<{ value: string }>("SELECT value FROM metadata WHERE key = ?", key)
        .toArray()[0]?.value ?? null
    );
  }

  private setMeta(key: string, value: string): void {
    this.ctx.storage.sql.exec(
      "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      key,
      value,
    );
  }

  private setting(value: string, fallback: number): number {
    return positiveNumber(value) ?? fallback;
  }

  private leaseTimeoutMs(): number {
    return this.setting(this.env.GLOBAL_LEASE_TIMEOUT_SECONDS, 180) * 1000;
  }

  private currentMaxInflight(): number {
    const configured = Math.floor(this.setting(this.env.GLOBAL_MAX_INFLIGHT, 24));
    const stored = Number(this.getMeta("global_max_inflight_current"));
    if (!Number.isFinite(stored) || stored < 1) {
      this.setMeta("global_max_inflight_current", String(configured));
      return configured;
    }
    const current = Math.min(configured, Math.floor(stored));
    if (current !== stored) this.setMeta("global_max_inflight_current", String(current));
    return current;
  }

  private activeLeaseCount(now: number): number {
    return this.ctx.storage.sql
      .exec<{ count: number }>(
        "SELECT COUNT(*) AS count FROM leases WHERE reported_at IS NULL AND created_at > ?",
        now - this.leaseTimeoutMs(),
      )
      .one().count;
  }

  private recentOutcomeStats(): { count: number; unavailable: number; successes: number } {
    const rows = this.ctx.storage.sql
      .exec<{ http_status: number }>(
        "SELECT http_status FROM recent_outcomes ORDER BY id DESC LIMIT 20",
      )
      .toArray();
    return {
      count: rows.length,
      unavailable: rows.filter((row) => row.http_status === 503).length,
      successes: rows.filter((row) => row.http_status === 200).length,
    };
  }

  private recordOutcome(httpStatus: number, now: number): void {
    this.ctx.storage.sql.exec(
      "INSERT INTO recent_outcomes(http_status, reported_at) VALUES(?, ?)",
      httpStatus,
      now,
    );
    this.ctx.storage.sql.exec(
      "DELETE FROM recent_outcomes WHERE id NOT IN (SELECT id FROM recent_outcomes ORDER BY id DESC LIMIT 50)",
    );
  }

  private updateGlobalController(httpStatus: number, quotaType: string, now: number): void {
    const current = this.currentMaxInflight();
    const configured = Math.floor(this.setting(this.env.GLOBAL_MAX_INFLIGHT, 24));
    const lastChange = Number(this.getMeta("global_controller_changed_at") ?? "0");
    const stats = this.recentOutcomeStats();
    const ratio = stats.count === 0 ? 0 : stats.unavailable / stats.count;

    if (httpStatus === 429 && (quotaType === "unknown" || quotaType === "tpm" || quotaType === "spend")) {
      const cooldownUntil = now + randomInteger(2_000, 5_000);
      this.setMeta(
        "global_cooldown_until",
        String(Math.max(Number(this.getMeta("global_cooldown_until") ?? "0"), cooldownUntil)),
      );
    }

    if (
      stats.count >= 5 &&
      ratio >= 0.2 &&
      now - lastChange >= 30_000
    ) {
      const stage = Math.min(3, Number(this.getMeta("global_adaptive_stage") ?? "0") + 1);
      const next = Math.min(configured, current > 12 ? 12 : 8);
      this.setMeta("global_max_inflight_current", String(next));
      this.setMeta("global_adaptive_stage", String(stage));
      this.setMeta("global_controller_changed_at", String(now));
      const [minimum, maximum] = stage === 1 ? [5, 15] : stage === 2 ? [15, 30] : [30, 60];
      const cooldownUntil = now + randomInteger(minimum * 1000, maximum * 1000);
      this.setMeta(
        "global_cooldown_until",
        String(Math.max(Number(this.getMeta("global_cooldown_until") ?? "0"), cooldownUntil)),
      );
      return;
    }

    if (
      httpStatus === 200 &&
      stats.count >= 20 &&
      stats.successes >= 19 &&
      current < configured &&
      now - lastChange >= 60_000
    ) {
      const next = current <= 8 ? 12 : configured;
      this.setMeta("global_max_inflight_current", String(Math.min(configured, next)));
      this.setMeta("global_adaptive_stage", "0");
      this.setMeta("global_controller_changed_at", String(now));
    }
  }

  private ensurePacificDay(): string {
    const today = pacificDate();
    if (this.getMeta("pacific_day") !== today) {
      const stale = this.ctx.storage.sql
        .exec<{ group_id: string }>(
          "SELECT group_id FROM projects WHERE pacific_date <> ?",
          today,
        )
        .toArray();
      for (const row of stale) {
        this.ctx.storage.sql.exec(
          `UPDATE projects
           SET requests_today = 0, pacific_date = ?, cooldown_until = 0,
               daily_exhausted = 0, success_count = 0, rate_limited_count = 0,
               error_count = 0
           WHERE group_id = ?`,
          today,
          row.group_id,
        );
        this.ctx.storage.sql.exec("DELETE FROM recent_requests WHERE group_id = ?", row.group_id);
      }
      if (stale.length > 0) {
        this.setMeta("rpm_per_project", "15");
        this.setMeta("rpd_per_project", "500");
      }
      this.setMeta("pacific_day", today);
    }
    if (this.getMeta("leases_pruned_day") !== today) {
      this.ctx.storage.sql.exec(
        "DELETE FROM leases WHERE created_at < ?",
        Date.now() - 7 * 24 * 60 * 60 * 1000,
      );
      this.setMeta("leases_pruned_day", today);
    }
    return today;
  }

  private configuredGroups(): string[] {
    try {
      const parsed = JSON.parse(this.getMeta("groups_json") ?? "[]");
      return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string") : [];
    } catch {
      return [];
    }
  }

  configure(body: unknown): RpcResult {
    const input = asRecord(body);
    const groups = input?.keyGroups;
    const rpm = positiveNumber(input?.rpmPerProject);
    const rpd = positiveNumber(input?.rpdPerProject);
    const expectedCount = Number(this.env.EXPECTED_KEY_COUNT || "66");
    if (
      !Array.isArray(groups) ||
      groups.length !== expectedCount ||
      groups.some((item) => typeof item !== "string" || item.trim().length === 0 || item.length > 128)
    ) {
      return { status: 400, body: { error: "invalid_project_mapping", expectedKeyCount: expectedCount } };
    }
    const groupList = groups as string[];
    if (new Set(groupList).size !== expectedCount) {
      return {
        status: 409,
        body: {
          error: "expected_one_project_per_key",
          keyCount: groupList.length,
          projectCount: new Set(groupList).size,
        },
      };
    }
    if (rpm === null || rpd === null || !Number.isInteger(rpd)) {
      return { status: 400, body: { error: "invalid_quota_limits" } };
    }

    const storedGroups = this.getMeta("groups_json");
    const newGroups = JSON.stringify(groupList);
    if (storedGroups !== null && storedGroups !== newGroups) {
      return {
        status: 409,
        body: {
          error: "project_mapping_changed",
          message: "Key order or project mapping differs from the initialized quota pool.",
        },
      };
    }

    const oldRpm = this.getMeta("rpm_per_project");
    const oldRpd = this.getMeta("rpd_per_project");
    const activeLeases = this.activeLeaseCount(Date.now());
    if (
      activeLeases > 0 &&
      ((oldRpm !== null && Number(oldRpm) !== rpm) ||
        (oldRpd !== null && Number(oldRpd) !== rpd))
    ) {
      return {
        status: 409,
        body: { error: "quota_limits_in_use", message: "Quota limits cannot change while requests are in flight." },
      };
    }

    const today = this.ensurePacificDay();
    if (storedGroups === null) {
      for (const [ordinal, group] of groupList.entries()) {
        this.ctx.storage.sql.exec(
          `INSERT OR IGNORE INTO projects(group_id, ordinal, pacific_date)
           VALUES(?, ?, ?)`,
          group,
          ordinal,
          today,
        );
        this.ctx.storage.sql.exec(
          "INSERT OR REPLACE INTO key_map(key_index, group_id) VALUES(?, ?)",
          ordinal,
          group,
        );
      }
      this.setMeta("groups_json", newGroups);
      this.setMeta("cursor", "0");
    }
    this.setMeta("rpm_per_project", String(rpm));
    this.setMeta("rpd_per_project", String(rpd));
    const projectCount = this.ctx.storage.sql
      .exec<{ count: number }>("SELECT COUNT(*) AS count FROM projects")
      .one().count;
    return {
      status: 200,
      body: {
        configured: true,
        keyCount: groupList.length,
        projectCount,
        pacificDate: today,
        rpmPerProject: rpm,
        rpdPerProject: rpd,
      },
    };
  }

  importLegacyState(body: unknown): RpcResult {
    if (this.getMeta("legacy_state_imported") === "1") {
      return { status: 409, body: { error: "state_already_imported" } };
    }
    const input = asRecord(body);
    const projects = asRecord(input?.projects);
    const keys = asRecord(input?.keys);
    const expectedCount = Number(this.env.EXPECTED_KEY_COUNT || "66");
    const today = this.ensurePacificDay();
    if (
      input?.version !== 1 ||
      input?.pacific_date !== today ||
      !projects ||
      !keys ||
      Object.keys(projects).length !== expectedCount ||
      Object.keys(keys).length !== expectedCount
    ) {
      return {
        status: 409,
        body: {
          error: "legacy_state_not_importable",
          expectedPacificDate: today,
          expectedProjectCount: expectedCount,
        },
      };
    }
    const existing = this.ctx.storage.sql
      .exec<{ count: number }>("SELECT COUNT(*) AS count FROM projects")
      .one().count;
    if (existing > 0) return { status: 409, body: { error: "pool_already_initialized" } };

    const orderedGroups: string[] = [];
    const importedProjects: Array<{
      group: string;
      requestsToday: number;
      cooldownUntil: number;
      dailyExhausted: number;
      successes: number;
      rateLimited: number;
      errors: number;
      recentRequests: number[];
    }> = [];
    const now = Date.now();
    for (let keyNumber = 1; keyNumber <= expectedCount; keyNumber += 1) {
      const key = asRecord(keys[String(keyNumber)]);
      const group = key?.project;
      if (typeof group !== "string" || group.trim().length === 0) {
        return { status: 400, body: { error: "invalid_legacy_key_mapping" } };
      }
      const project = asRecord(projects[group]);
      if (!project) return { status: 400, body: { error: "legacy_project_state_missing" } };
      orderedGroups.push(group);
      importedProjects.push({
        group,
        requestsToday: Math.max(0, Math.floor(Number(project.requests_today) || 0)),
        cooldownUntil: dateEpoch(project.cooldown_until),
        dailyExhausted: project.daily_exhausted ? 1 : 0,
        successes: Math.max(0, Math.floor(Number(project.success) || 0)),
        rateLimited: Math.max(0, Math.floor(Number(project.http_429) || 0)),
        errors: Math.max(0, Math.floor(Number(project.errors) || 0)),
        recentRequests: (Array.isArray(project.recent_requests) ? project.recent_requests : [])
          .map(dateEpoch)
          .filter((requestedAt) => requestedAt > now - 60_000 && requestedAt <= now + 5_000),
      });
    }
    if (new Set(orderedGroups).size !== expectedCount) {
      return { status: 409, body: { error: "legacy_state_projects_not_unique" } };
    }

    for (const [ordinal, project] of importedProjects.entries()) {
      this.ctx.storage.sql.exec(
        `INSERT INTO projects(
           group_id, ordinal, requests_today, pacific_date, cooldown_until,
           daily_exhausted, success_count, rate_limited_count, error_count
         ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        project.group,
        ordinal,
        project.requestsToday,
        today,
        project.cooldownUntil,
        project.dailyExhausted,
        project.successes,
        project.rateLimited,
        project.errors,
      );
      this.ctx.storage.sql.exec(
        "INSERT INTO key_map(key_index, group_id) VALUES(?, ?)",
        ordinal,
        project.group,
      );
      for (const requestedAt of project.recentRequests) {
        this.ctx.storage.sql.exec(
          "INSERT INTO recent_requests(group_id, requested_at) VALUES(?, ?)",
          project.group,
          requestedAt,
        );
      }
    }
    this.setMeta("groups_json", JSON.stringify(orderedGroups));
    this.setMeta("cursor", "0");
    this.setMeta("legacy_state_imported", "1");
    const totalRequests = this.ctx.storage.sql
      .exec<{ total: number }>("SELECT COALESCE(SUM(requests_today), 0) AS total FROM projects")
      .one().total;
    const activeCooldowns = this.ctx.storage.sql
      .exec<{ count: number }>("SELECT COUNT(*) AS count FROM projects WHERE cooldown_until > ?", now)
      .one().count;
    return {
      status: 200,
      body: {
        imported: true,
        keyCount: orderedGroups.length,
        projectCount: orderedGroups.length,
        pacificDate: today,
        requestsToday: totalRequests,
        activeCooldowns,
      },
    };
  }

  acquire(): RpcResult {
    const now = Date.now();
    this.ensurePacificDay();
    const groups = this.configuredGroups();
    if (groups.length === 0) return { status: 503, body: { error: "quota_pool_not_configured" } };

    const globalCooldownUntil = Number(this.getMeta("global_cooldown_until") ?? "0");
    if (globalCooldownUntil > now) {
      return {
        status: 202,
        body: {
          waitMs: Math.min(30_000, globalCooldownUntil - now + randomInteger(0, 500)),
          reason: "global_cooldown",
        },
      };
    }

    const maxInflight = this.currentMaxInflight();
    const activeLeases = this.activeLeaseCount(now);
    if (activeLeases >= maxInflight) {
      const leaseTimeout = this.leaseTimeoutMs();
      const firstExpiry = this.ctx.storage.sql
        .exec<{ created_at: number }>(
          `SELECT MIN(created_at) AS created_at FROM leases
           WHERE reported_at IS NULL AND created_at > ?`,
          now - leaseTimeout,
        )
        .toArray()[0]?.created_at;
      const untilExpiry = firstExpiry === undefined ? 1_000 : firstExpiry + leaseTimeout - now;
      return {
        status: 202,
        body: {
          waitMs: Math.max(250, Math.min(5_000, untilExpiry)),
          reason: "max_inflight",
        },
      };
    }

    const globalRps = this.setting(this.env.GLOBAL_RPS, 2);
    const globalBurst = this.setting(this.env.GLOBAL_BURST, 8);
    const previousTokens = this.getMeta("global_tokens");
    const previousUpdatedAt = Number(this.getMeta("global_tokens_updated_at") ?? String(now));
    const elapsedSeconds = Math.max(0, now - previousUpdatedAt) / 1000;
    const tokens = Math.min(
      globalBurst,
      (previousTokens === null ? globalBurst : Number(previousTokens)) + elapsedSeconds * globalRps,
    );
    if (tokens < 1) {
      return {
        status: 202,
        body: {
          waitMs: Math.max(2_000, Math.ceil(((1 - tokens) / globalRps) * 1000)) + randomInteger(0, 2_000),
          reason: "global_rate",
        },
      };
    }

    const rpm = Number(this.getMeta("rpm_per_project") ?? "15");
    const rpd = Number(this.getMeta("rpd_per_project") ?? "500");
    this.ctx.storage.sql.exec("DELETE FROM recent_requests WHERE requested_at <= ?", now - 60_000);
    const cursor = Number(this.getMeta("cursor") ?? "0") % groups.length;
    const candidates: Array<{
      group: string;
      ordinal: number;
      keyIndex: number;
      requestsToday: number;
      readyAt: number;
      distance: number;
    }> = [];

    for (let ordinal = 0; ordinal < groups.length; ordinal += 1) {
      const group = groups[ordinal];
      const project = this.ctx.storage.sql
        .exec<ProjectRow>("SELECT * FROM projects WHERE group_id = ?", group)
        .toArray()[0];
      if (!project || project.daily_exhausted) continue;
      if (project.requests_today >= rpd) {
        this.ctx.storage.sql.exec(
          "UPDATE projects SET daily_exhausted = 1 WHERE group_id = ?",
          group,
        );
        continue;
      }
      const keys = this.ctx.storage.sql
        .exec<{ key_index: number }>(
          "SELECT key_index FROM key_map WHERE group_id = ? ORDER BY key_index",
          group,
        )
        .toArray();
      if (keys.length === 0) continue;
      const keyIndex = keys.find((item) => item.key_index > project.last_key_index)?.key_index ?? keys[0].key_index;
      const recent = this.ctx.storage.sql
        .exec<{ requested_at: number }>(
          "SELECT requested_at FROM recent_requests WHERE group_id = ? ORDER BY requested_at",
          group,
        )
        .toArray();
      const interval = (60_000 / rpm) * 1.08;
      let readyAt = Math.max(now, project.cooldown_until);
      if (recent.length > 0) readyAt = Math.max(readyAt, recent[recent.length - 1].requested_at + interval);
      if (recent.length >= rpm) readyAt = Math.max(readyAt, recent[0].requested_at + 60_050);
      candidates.push({
        group,
        ordinal,
        keyIndex,
        requestsToday: project.requests_today,
        readyAt,
        distance: (ordinal - cursor + groups.length) % groups.length,
      });
    }

    if (candidates.length === 0) {
      return {
        status: 429,
        body: { error: "quota_pool_exhausted", pacificDate: pacificDate() },
      };
    }
    candidates.sort((left, right) =>
      left.readyAt - right.readyAt ||
      left.requestsToday - right.requestsToday ||
      left.distance - right.distance,
    );
    const selected = candidates[0];
    if (selected.readyAt > now) {
      return {
        status: 202,
        body: {
          waitMs: Math.max(250, Math.min(30_000, Math.ceil(selected.readyAt - now))),
          reason: "project_quota",
        },
      };
    }

    const leaseId = crypto.randomUUID();
    this.setMeta("global_tokens", String(tokens - 1));
    this.setMeta("global_tokens_updated_at", String(now));
    this.ctx.storage.sql.exec(
      `INSERT INTO leases(lease_id, group_id, key_index, created_at)
       VALUES(?, ?, ?, ?)`,
      leaseId,
      selected.group,
      selected.keyIndex,
      now,
    );
    this.ctx.storage.sql.exec(
      "INSERT INTO recent_requests(group_id, requested_at) VALUES(?, ?)",
      selected.group,
      now,
    );
    this.ctx.storage.sql.exec(
      `UPDATE projects
       SET requests_today = requests_today + 1, last_key_index = ?, last_assigned_at = ?
       WHERE group_id = ?`,
      selected.keyIndex,
      now,
      selected.group,
    );
    this.setMeta("cursor", String((selected.ordinal + 1) % groups.length));
    return {
      status: 200,
      body: {
        projectIndex: selected.ordinal,
        keyIndex: selected.keyIndex,
        leaseId,
        globalMaxInflight: maxInflight,
      },
    };
  }

  report(body: unknown): RpcResult {
    const input = asRecord(body);
    const leaseId = input?.leaseId;
    const httpStatus = input?.httpStatus;
    const cooldownSeconds = positiveNumber(input?.cooldownSeconds) ?? 0;
    const dailyExhausted = input?.dailyExhausted === true;
    const allowedQuotaTypes = new Set(["unknown", "rpm", "tpm", "rpd", "spend"]);
    const quotaType = typeof input?.quotaType === "string" && allowedQuotaTypes.has(input.quotaType)
      ? input.quotaType
      : "unknown";
    if (
      typeof leaseId !== "string" ||
      leaseId.length > 80 ||
      typeof httpStatus !== "number" ||
      !Number.isInteger(httpStatus) ||
      httpStatus < 0 ||
      httpStatus > 599
    ) {
      return { status: 400, body: { error: "invalid_report" } };
    }
    const lease = this.ctx.storage.sql
      .exec<LeaseRow>(
        "SELECT lease_id, group_id, key_index, created_at, http_status, daily_exhausted, reported_at FROM leases WHERE lease_id = ?",
        leaseId,
      )
      .toArray()[0];
    if (!lease) return { status: 404, body: { error: "lease_not_found" } };
    if (lease.reported_at !== null) return { status: 200, body: { reported: true, duplicate: true } };

    const now = Date.now();
    this.recordOutcome(httpStatus, now);
    this.updateGlobalController(httpStatus, quotaType, now);
    const sameQuotaDay = pacificDate(lease.created_at) === pacificDate(now);
    if (!sameQuotaDay) {
      this.ctx.storage.sql.exec(
        "UPDATE leases SET http_status = ?, daily_exhausted = ?, reported_at = ? WHERE lease_id = ?",
        httpStatus,
        dailyExhausted ? 1 : 0,
        now,
        leaseId,
      );
      return { status: 200, body: { reported: true, staleLease: true } };
    }
    if (httpStatus === 429) {
      if (dailyExhausted) {
        this.ctx.storage.sql.exec(
          `UPDATE projects
           SET daily_exhausted = 1,
               requests_today = MAX(requests_today, ?),
               rate_limited_count = rate_limited_count + 1
           WHERE group_id = ?`,
          Number(this.getMeta("rpd_per_project") ?? "500"),
          lease.group_id,
        );
      } else {
        const cooldownUntil = now + Math.min(86_400, Math.max(1, cooldownSeconds)) * 1000;
        this.ctx.storage.sql.exec(
          `UPDATE projects
           SET cooldown_until = MAX(cooldown_until, ?),
               rate_limited_count = rate_limited_count + 1
           WHERE group_id = ?`,
          cooldownUntil,
          lease.group_id,
        );
      }
    } else if (httpStatus === 200) {
      this.ctx.storage.sql.exec(
        "UPDATE projects SET success_count = success_count + 1 WHERE group_id = ?",
        lease.group_id,
      );
    } else {
      this.ctx.storage.sql.exec(
        "UPDATE projects SET error_count = error_count + 1 WHERE group_id = ?",
        lease.group_id,
      );
    }
    this.ctx.storage.sql.exec(
      "UPDATE leases SET http_status = ?, daily_exhausted = ?, reported_at = ? WHERE lease_id = ?",
      httpStatus,
      dailyExhausted ? 1 : 0,
      now,
      leaseId,
    );
    return { status: 200, body: { reported: true, duplicate: false } };
  }

  status(): RpcResult {
    const now = Date.now();
    const today = this.ensurePacificDay();
    const projects = this.ctx.storage.sql
      .exec<ProjectRow>("SELECT * FROM projects ORDER BY ordinal")
      .toArray();
    const recentCounts = new Map(
      this.ctx.storage.sql
        .exec<{ group_id: string; count: number }>(
          "SELECT group_id, COUNT(*) AS count FROM recent_requests GROUP BY group_id",
        )
        .toArray()
        .map((row) => [row.group_id, row.count]),
    );
    const activeLeases = this.activeLeaseCount(now);
    const outcomeStats = this.recentOutcomeStats();
    const globalMaxInflight = this.currentMaxInflight();
    const configuredMaxInflight = Math.floor(this.setting(this.env.GLOBAL_MAX_INFLIGHT, 24));
    const globalCooldownUntil = Number(this.getMeta("global_cooldown_until") ?? "0");
    const recentActiveProjects = recentCounts.size;
    return {
      status: 200,
      body: {
        pacificDate: today,
        keyCount: this.ctx.storage.sql.exec<{ count: number }>("SELECT COUNT(*) AS count FROM key_map").one().count,
        projectCount: projects.length,
        rpmPerProject: Number(this.getMeta("rpm_per_project") ?? "15"),
        rpdPerProject: Number(this.getMeta("rpd_per_project") ?? "500"),
        requestsToday: projects.reduce((sum, project) => sum + project.requests_today, 0),
        activeLeases,
        global: {
          maxInflight: globalMaxInflight,
          configuredMaxInflight,
          activeLeases,
          requestsPerSecond: this.setting(this.env.GLOBAL_RPS, 2),
          burst: this.setting(this.env.GLOBAL_BURST, 8),
          leaseTimeoutSeconds: Math.floor(this.leaseTimeoutMs() / 1000),
          cooldownUntil: globalCooldownUntil > now ? globalCooldownUntil : null,
          cooldownRemainingMs: Math.max(0, globalCooldownUntil - now),
          adaptiveStage: Number(this.getMeta("global_adaptive_stage") ?? "0"),
          recentOutcomes: outcomeStats.count,
          recent503Ratio: outcomeStats.count === 0 ? 0 : outcomeStats.unavailable / outcomeStats.count,
          recentSuccesses: outcomeStats.successes,
          activeProjects: recentActiveProjects,
        },
        projects: projects.map((project) => ({
          projectIndex: project.ordinal,
          requestsToday: project.requests_today,
          recentRequests: recentCounts.get(project.group_id) ?? 0,
          cooldownUntil: project.cooldown_until || null,
          dailyExhausted: project.daily_exhausted === 1,
          successes: project.success_count,
          rateLimited: project.rate_limited_count,
          errors: project.error_count,
        })),
      },
    };
  }
}
