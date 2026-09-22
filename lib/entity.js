// The dashboard as a Slack Work Object ("Content item" entity).

export const CANONICAL_URL =
  process.env.DASHBOARD_URL || 'https://gati-mobility-sentiment-tracker.vercel.app/';

// Hosts whose pasted links should unfurl. Keep in sync with `unfurl_domains` in the app manifest.
const VERCEL_HOST = new URL(CANONICAL_URL).hostname;
const PAGES_HOST = 'tharunkm-104.github.io';
const PAGES_PATH = '/gati-mobility-sentiment-tracker';

export const EXTERNAL_REF_ID = 'gati-mobility-sentiment-tracker'; // must never change (related-conversations tracking)

export function isDashboardLink(rawUrl) {
  let u;
  try {
    u = new URL(rawUrl);
  } catch {
    return false;
  }
  if (u.hostname === VERCEL_HOST) return !u.pathname.startsWith('/api/');
  if (u.hostname === PAGES_HOST) return u.pathname.startsWith(PAGES_PATH);
  return false;
}

/**
 * Builds the entity metadata.
 *  - unfurl (chat.unfurl):            pass appUnfurlUrl, no previewUrl  -> declares embed support
 *  - flexpane (entity.presentDetails): pass previewUrl, no appUnfurlUrl   -> Slack loads it in the iframe
 */
export function buildEntity({ appUnfurlUrl, previewUrl } = {}) {
  const fullSizePreview = {
    is_supported: true,
    mime_type: 'application/vnd.slack-embed',
    ...(previewUrl ? { preview_url: previewUrl } : {}),
  };

  const entity = {
    url: CANONICAL_URL,
    external_ref: { id: EXTERNAL_REF_ID },
    entity_type: 'slack#/entities/item',
    entity_payload: {
      attributes: {
        title: { text: 'GATI Labour Mobility Sentiment Tracker' },
        display_type: 'Live dashboard',
        product_name: 'GATI Foundation',
        full_size_preview: fullSizePreview,
      },
    },
  };
  if (appUnfurlUrl) entity.app_unfurl_url = appUnfurlUrl;
  return entity;
}
