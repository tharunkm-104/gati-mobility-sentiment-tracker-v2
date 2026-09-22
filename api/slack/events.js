import { WebClient } from '@slack/web-api';
import { verifySlackSignature } from '../../lib/verify.js';
import { CANONICAL_URL, EXTERNAL_REF_ID, buildEntity, isDashboardLink } from '../../lib/entity.js';

// SLACK_API_URL is only used by the local test suite (points the client at a mock server).
const slack = () =>
  new WebClient(process.env.SLACK_BOT_TOKEN, process.env.SLACK_API_URL ? { slackApiUrl: process.env.SLACK_API_URL } : {});

async function onLinkShared(event) {
  const entities = (event.links || [])
    .filter((l) => isDashboardLink(l.url))
    .map((l) => buildEntity({ appUnfurlUrl: l.url })); // no preview_url yet: declares embed support only

  if (entities.length === 0) return;

  // channel + ts always work here, even for composer events (see link_shared docs).
  await slack().chat.unfurl({
    channel: event.channel,
    ts: event.message_ts,
    metadata: { entities },
  });
}

async function onEntityDetailsRequested(event) {
  if (event.external_ref?.id !== EXTERNAL_REF_ID) return;

  await slack().entity.presentDetails({
    trigger_id: event.trigger_id,
    metadata: buildEntity({ previewUrl: CANONICAL_URL }), // Slack loads this in the sandboxed iframe
  });
}

export async function POST(request) {
  // Raw body is required for signature verification. Never JSON.parse before verifying.
  const rawBody = await request.text();

  const valid = verifySlackSignature({
    signingSecret: process.env.SLACK_SIGNING_SECRET,
    timestamp: request.headers.get('x-slack-request-timestamp'),
    signature: request.headers.get('x-slack-signature'),
    rawBody,
  });
  if (!valid) return new Response('invalid signature', { status: 401 });

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return new Response('bad request', { status: 400 });
  }

  if (payload.type === 'url_verification') {
    return Response.json({ challenge: payload.challenge });
  }

  if (payload.type === 'event_callback') {
    const event = payload.event || {};
    try {
      if (event.type === 'link_shared') await onLinkShared(event);
      else if (event.type === 'entity_details_requested') await onEntityDetailsRequested(event);
    } catch (err) {
      // Log and still return 200 so Slack does not retry-storm us.
      console.error(`Slack handler error (${event.type}):`, err?.data || err);
    }
  }

  return new Response('', { status: 200 });
}
