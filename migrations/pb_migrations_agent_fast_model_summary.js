/* global migrate, BoolField, JSONField */
// Additive migration for fast-model title generation, periodic summary, and timeline milestones.
migrate(app => {
  const jobs = app.findCollectionByNameOrId('aw_gemini_jobs');
  const add = (collection, field) => {
    if (!collection.fields.getByName(field.name)) collection.fields.add(field);
  };
  add(jobs, new BoolField({ name: 'titleGenerated' }));
  app.save(jobs);

  const messages = app.findCollectionByNameOrId('aw_gemini_messages');
  add(messages, new JSONField({ name: 'summary', maxSize: 131072 }));
  add(messages, new JSONField({ name: 'timeline', maxSize: 131072 }));
  app.save(messages);
}, () => {
  // Preserve captured data on rollback.
});
