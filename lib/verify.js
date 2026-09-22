import crypto from 'node:crypto';

const MAX_SKEW_SECONDS = 60 * 5; // Slack recommends rejecting requests older than 5 minutes

/**
 * Verifies a Slack request signature (v0 scheme).
 * `rawBody` MUST be the exact, unparsed request body string.
 */
export function verifySlackSignature({
  signingSecret,
  timestamp,
  signature,
  rawBody,
  nowMs = Date.now(),
}) {
  if (!signingSecret || !timestamp || !signature || typeof rawBody !== 'string') return false;

  const ts = Number(timestamp);
  if (!Number.isFinite(ts) || Math.abs(nowMs / 1000 - ts) > MAX_SKEW_SECONDS) return false;

  const expected =
    'v0=' +
    crypto.createHmac('sha256', signingSecret).update(`v0:${timestamp}:${rawBody}`).digest('hex');

  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
