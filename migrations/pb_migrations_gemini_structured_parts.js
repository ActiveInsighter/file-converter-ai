/* global migrate, TextField, JSONField, NumberField */
// Additive and repeatable. Retain the existing owner rules and stream identities.
migrate(app => {
  const messages = app.findCollectionByNameOrId('aw_gemini_messages');
  const add = (collection, field) => {
    if (!collection.fields.getByName(field.name)) collection.fields.add(field);
  };
  add(messages, new TextField({ name: 'protocol', max: 64 }));
  add(messages, new TextField({ name: 'thinkingSummary', max: 2097152 }));
  add(messages, new TextField({ name: 'finishReason', max: 64 }));
  add(messages, new NumberField({ name: 'uploadSequence', onlyInt: true, min: 0 }));
  for (const [name, maxSize] of Object.entries({ parts: 2097152, toolCalls: 1048576,
    codeExecutions: 1048576, annotations: 524288, grounding: 524288, structuredOutput: 524288,
    execution: 65536, usage: 65536, response: 2097152 })) {
    add(messages, new JSONField({ name, maxSize }));
  }
  app.save(messages);
  const parts = app.findCollectionByNameOrId('aw_agent_parts');
  add(parts, new TextField({ name: 'toolKind', max: 32 }));
  add(parts, new JSONField({ name: 'payload', maxSize: 131072 }));
  const type = parts.fields.getByName('type');
  type.values = [...new Set([...type.values, 'image', 'annotations'])];
  app.save(parts);
}, () => {
  // Preserve captured data on code rollback. Older executors ignore the new fields.
});
