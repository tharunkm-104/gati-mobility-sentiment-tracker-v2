import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import crypto from 'node:crypto';

const SECRET = 'test-signing-secret';
const calls = []; // requests received by the mock Slack API
let server;

before(async () => {
  server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      calls.push({ path: req.url, contentType: req.headers['content-type'], auth: req.headers.authorization, body });
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify({ ok: true }));
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  process.env.SLACK_API_URL = `http://127.0.0.1:${server.address().port}/api/`;
  process.env.SLACK_SIGNING_SECRET = SECRET;
  process.env.SLACK_BOT_TOKEN = 'xoxb-test';
});
after(() => server.close());

const { POST } = await import('../api/slack/events.js');
const { verifySlackSignature } = await import('../lib/verify.js');

function signed(bodyObj, { secret = SECRET, ts = Math.floor(Date.now() / 1000) } = {}) {
  const raw = JSON.stringify(bodyObj);
  const sig = 'v0=' + crypto.createHmac('sha256', secret).update(`v0:${ts}:${raw}`).digest('hex');
  return new Request('https://x.test/api/slack/events', {
    method: 'POST',
    headers: { 'x-slack-request-timestamp': String(ts), 'x-slack-signature': sig, 'content-type': 'application/json' },
    body: raw,
  });
}

// Parse a form-encoded or JSON Slack API request body into an object, decoding JSON-string params.
function decode(call) {
  if (call.contentType?.includes('application/json')) return JSON.parse(call.body);
  const o = Object.fromEntries(new URLSearchParams(call.body));
  for (const k of ['metadata']) if (o[k]) o[k] = JSON.parse(o[k]);
  return o;
}

test('signature: valid, tampered, wrong secret, stale', () => {
  const ts = Math.floor(Date.now() / 1000);
  const raw = '{"a":1}';
  const sig = 'v0=' + crypto.createHmac('sha256', SECRET).update(`v0:${ts}:${raw}`).digest('hex');
  assert.equal(verifySlackSignature({ signingSecret: SECRET, timestamp: ts, signature: sig, rawBody: raw }), true);
  assert.equal(verifySlackSignature({ signingSecret: SECRET, timestamp: ts, signature: sig, rawBody: raw + ' ' }), false);
  assert.equal(verifySlackSignature({ signingSecret: 'nope', timestamp: ts, signature: sig, rawBody: raw }), false);
  assert.equal(verifySlackSignature({ signingSecret: SECRET, timestamp: ts - 3600, signature: sig, rawBody: raw, nowMs: Date.now() }), false);
  assert.equal(verifySlackSignature({ signingSecret: SECRET, timestamp: ts, signature: 'v0=short', rawBody: raw }), false);
});

test('rejects unsigned / badly signed requests with 401 and makes no Slack calls', async () => {
  calls.length = 0;
  const res = await POST(signed({ type: 'event_callback', event: { type: 'link_shared' } }, { secret: 'wrong' }));
  assert.equal(res.status, 401);
  const res2 = await POST(new Request('https://x.test/api/slack/events', { method: 'POST', body: '{}' }));
  assert.equal(res2.status, 401);
  assert.equal(calls.length, 0);
});

test('url_verification returns the challenge', async () => {
  const res = await POST(signed({ type: 'url_verification', challenge: 'abc123' }));
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { challenge: 'abc123' });
});

test('link_shared -> chat.unfurl with embed-declaring entity (no preview_url yet)', async () => {
  calls.length = 0;
  const url = 'https://gati-mobility-sentiment-tracker.vercel.app/';
  const res = await POST(signed({
    type: 'event_callback',
    event: { type: 'link_shared', channel: 'C123', message_ts: '1700000000.000100', links: [{ domain: 'gati-mobility-sentiment-tracker.vercel.app', url }] },
  }));
  assert.equal(res.status, 200);
  assert.equal(calls.length, 1);
  assert.match(calls[0].path, /chat\.unfurl$/);
  assert.equal(calls[0].auth, 'Bearer xoxb-test');
  const p = decode(calls[0]);
  assert.equal(p.channel, 'C123');
  assert.equal(p.ts, '1700000000.000100');
  const e = p.metadata.entities[0];
  assert.equal(e.app_unfurl_url, url);
  assert.equal(e.entity_type, 'slack#/entities/item');
  assert.equal(e.external_ref.id, 'gati-mobility-sentiment-tracker');
  assert.deepEqual(e.entity_payload.attributes.full_size_preview, { is_supported: true, mime_type: 'application/vnd.slack-embed' });
  assert.equal('preview_url' in e.entity_payload.attributes.full_size_preview, false);
  console.log('    chat.unfurl content-type:', calls[0].contentType);
});

test('link_shared for the GitHub Pages URL also unfurls; unrelated links do not', async () => {
  calls.length = 0;
  const gh = 'https://tharunkm-104.github.io/gati-mobility-sentiment-tracker/';
  await POST(signed({ type: 'event_callback', event: { type: 'link_shared', channel: 'C1', message_ts: '1.1', links: [
    { domain: 'github.io', url: gh },
    { domain: 'github.io', url: 'https://tharunkm-104.github.io/some-other-repo/' },
  ] } }));
  assert.equal(calls.length, 1);
  const ents = decode(calls[0]).metadata.entities;
  assert.equal(ents.length, 1);
  assert.equal(ents[0].app_unfurl_url, gh);

  calls.length = 0;
  await POST(signed({ type: 'event_callback', event: { type: 'link_shared', channel: 'C1', message_ts: '1.1', links: [{ domain: 'example.com', url: 'https://example.com/' }] } }));
  assert.equal(calls.length, 0);
});

test('entity_details_requested -> entity.presentDetails with preview_url', async () => {
  calls.length = 0;
  const res = await POST(signed({
    type: 'event_callback',
    event: { type: 'entity_details_requested', trigger_id: 'trig.123', external_ref: { id: 'gati-mobility-sentiment-tracker' }, entity_url: 'https://gati-mobility-sentiment-tracker.vercel.app/' },
  }));
  assert.equal(res.status, 200);
  assert.equal(calls.length, 1);
  assert.match(calls[0].path, /entity\.presentDetails$/);
  const p = decode(calls[0]);
  assert.equal(p.trigger_id, 'trig.123');
  const fsp = p.metadata.entity_payload.attributes.full_size_preview;
  assert.deepEqual(fsp, { is_supported: true, mime_type: 'application/vnd.slack-embed', preview_url: 'https://gati-mobility-sentiment-tracker.vercel.app/' });
  assert.equal('app_unfurl_url' in p.metadata, false);
  console.log('    presentDetails content-type:', calls[0].contentType);
});

test('entity_details_requested for a different entity is ignored', async () => {
  calls.length = 0;
  await POST(signed({ type: 'event_callback', event: { type: 'entity_details_requested', trigger_id: 't', external_ref: { id: 'something-else' } } }));
  assert.equal(calls.length, 0);
});
