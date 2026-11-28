/** `transcribing`: a call is transcribed, which only a running recorder does. */
export type TrayStatusVisual = 'capturing' | 'idle' | 'transcribing';

type NativeImageSource = Pick<
  Electron.NativeImage,
  'getSize' | 'isEmpty' | 'setTemplateImage' | 'toBitmap'
>;

type NativeImageTarget = Pick<Electron.NativeImage, 'addRepresentation' | 'setTemplateImage'>;

type NativeImageCodec<TImage extends NativeImageTarget> = {
  createEmpty: () => TImage;
  createFromPath: (path: string) => NativeImageSource;
};

export type TrayStatusIconRepresentation = {
  buffer: Buffer;
  height: number;
  width: number;
};

export function decodeTrayStatusIconRepresentation(params: {
  iconPath: string;
  nativeImage: Pick<NativeImageCodec<NativeImageTarget>, 'createFromPath'>;
}): TrayStatusIconRepresentation {
  const decoded = params.nativeImage.createFromPath(params.iconPath);
  if (decoded.isEmpty()) {
    throw new Error(`Tray status icon could not be decoded: ${params.iconPath}`);
  }
  const { height, width } = decoded.getSize();
  return {
    buffer: decoded.toBitmap(),
    height,
    width,
  };
}

export function createTrayStatusIcon(params: {
  iconPath: string;
  nativeImage: NativeImageCodec<Electron.NativeImage>;
  retinaIconPath: string;
  visual: TrayStatusVisual;
}): Electron.NativeImage;
export function createTrayStatusIcon<TImage extends NativeImageTarget>(params: {
  iconPath: string;
  nativeImage: NativeImageCodec<TImage>;
  retinaIconPath: string;
  visual: TrayStatusVisual;
}): TImage;
export function createTrayStatusIcon<TImage extends NativeImageTarget>(params: {
  iconPath: string;
  nativeImage: NativeImageCodec<TImage>;
  retinaIconPath: string;
  visual: TrayStatusVisual;
}): TImage {
  const image = params.nativeImage.createEmpty();
  image.addRepresentation({
    scaleFactor: 1,
    ...decodeTrayStatusIconRepresentation({
      iconPath: params.iconPath,
      nativeImage: params.nativeImage,
    }),
  });
  image.addRepresentation({
    scaleFactor: 2,
    ...decodeTrayStatusIconRepresentation({
      iconPath: params.retinaIconPath,
      nativeImage: params.nativeImage,
    }),
  });
  image.setTemplateImage(params.visual === 'idle');
  return image;
}
