/** The icon's side: three times the rail's 40px tile, so it stays sharp on a Retina display. */
const ICON_SIDE_PX = 128;
/** The backend's cap on the icon's bytes (routers/workspace_organizations.py). */
const MAX_ICON_BYTES = 64 * 1024;
const ICON_TYPE = 'image/webp';
const ICON_QUALITY = 0.9;

export type OrganizationIcon =
  | { kind: 'ok'; dataUrl: string }
  | { kind: 'unreadable' }
  | { kind: 'tooLarge' };

/** The bytes a base64 data URL holds. */
const dataUrlBytes = (dataUrl: string) => {
  const base64 = dataUrl.slice(dataUrl.indexOf(',') + 1);
  return Math.floor((base64.length * 3) / 4) - (base64.match(/=+$/u)?.[0].length ?? 0);
};

/**
 * A picked image as the organization's icon: its middle square, drawn at 128px and encoded as a
 * WebP data URL (transparency kept). An image the browser cannot decode is unreadable.
 */
export async function organizationIconFrom(file: Blob): Promise<OrganizationIcon> {
  let image: ImageBitmap;
  try {
    image = await createImageBitmap(file);
  } catch (error) {
    // Chromium refuses bytes it cannot decode with an InvalidStateError.
    if (error instanceof DOMException) return { kind: 'unreadable' };
    throw error;
  }
  const canvas = document.createElement('canvas');
  canvas.width = ICON_SIDE_PX;
  canvas.height = ICON_SIDE_PX;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('A 2D canvas is unavailable.');
  const side = Math.min(image.width, image.height);
  context.drawImage(
    image,
    (image.width - side) / 2,
    (image.height - side) / 2,
    side,
    side,
    0,
    0,
    ICON_SIDE_PX,
    ICON_SIDE_PX
  );
  image.close();
  const dataUrl = canvas.toDataURL(ICON_TYPE, ICON_QUALITY);
  return dataUrlBytes(dataUrl) > MAX_ICON_BYTES ? { kind: 'tooLarge' } : { kind: 'ok', dataUrl };
}
