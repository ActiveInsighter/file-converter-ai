/// <reference path="../pb_data/types.d.ts" />
/**
 * PocketBase schema migration for structured Gemini & Antigravity message data.
 *
 * Adds RikkaHub-aligned structured fields to `aw_gemini_messages` and
 * `aw_gemini_message_parts` without removing any legacy fields.
 *
 * Usage:
 *   Copy to your PocketBase `pb_migrations/` directory and restart PocketBase,
 *   or apply the fields manually in PocketBase Admin UI (see docs/pocketbase-schema-migration.md).
 */
migrate((app) => {
  const addFieldIfMissing = (collection, fieldConfig) => {
    const existing = collection.fields.getByName(fieldConfig.name);
    if (!existing) {
      collection.fields.add(new Field(fieldConfig));
    }
  };

  // 1. Update aw_gemini_messages
  const messages = app.findCollectionByNameOrId("aw_gemini_messages");
  const messageFields = [
    { name: "protocol", type: "text", max: 64 },
    { name: "parts", type: "json", maxSize: 2097152 },
    { name: "thinkingSummary", type: "text", max: 2097152 },
    { name: "toolCalls", type: "json", maxSize: 1048576 },
    { name: "codeExecutions", type: "json", maxSize: 1048576 },
    { name: "annotations", type: "json", maxSize: 524288 },
    { name: "grounding", type: "json", maxSize: 524288 },
    { name: "structuredOutput", type: "json", maxSize: 524288 },
    { name: "execution", type: "json", maxSize: 65536 },
    { name: "usage", type: "json", maxSize: 65536 },
    { name: "finishReason", type: "text", max: 64 },
    { name: "response", type: "json", maxSize: 2097152 },
    { name: "uploadSequence", type: "number" },
  ];
  for (const field of messageFields) {
    addFieldIfMissing(messages, field);
  }
  app.save(messages);

  // 2. Update aw_gemini_message_parts (if collection exists)
  try {
    const parts = app.findCollectionByNameOrId("aw_gemini_message_parts");
    const partFields = [
      { name: "key", type: "text", max: 128 },
      { name: "sequence", type: "number" },
      { name: "type", type: "text", max: 64 },
      { name: "status", type: "text", max: 32 },
      { name: "toolName", type: "text", max: 120 },
      { name: "toolKind", type: "text", max: 32 },
      { name: "callId", type: "text", max: 256 },
      { name: "providerType", type: "text", max: 120 },
      { name: "content", type: "text", max: 131072 },
      { name: "payload", type: "json", maxSize: 131072 },
      { name: "truncated", type: "bool" },
    ];
    for (const field of partFields) {
      addFieldIfMissing(parts, field);
    }
    app.save(parts);
  } catch (_) {
    // Collection may not be created yet; see docs/pocketbase-schema-migration.md
  }
}, (app) => {
  // Down migration intentionally preserves data; remove fields manually if desired.
});
