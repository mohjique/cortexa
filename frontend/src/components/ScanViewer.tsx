// Full-screen MRI viewer, so the doctor can read the scan before confirming or
// correcting the suspected bucket. Opens on the plain scan, not the Grad-CAM
// overlay: the doctor reads the image first, then sees where the model looked.
import { useEffect, useState } from "react";

export interface ViewerImage {
  key: string;
  label: string;
  src: string;
  note?: string;
}

const ZOOMS = [1, 1.5, 2, 3];

export default function ScanViewer({
  title,
  images,
  start = 0,
  onClose,
  footer,
}: {
  title: string;
  images: ViewerImage[];
  start?: number;
  onClose: () => void;
  footer?: React.ReactNode;
}) {
  const [i, setI] = useState(start);
  const [zoom, setZoom] = useState(1);
  const [bright, setBright] = useState(100);
  const [contrast, setContrast] = useState(100);
  const img = images[Math.min(i, images.length - 1)];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "+" || e.key === "=") setZoom((z) => ZOOMS[Math.min(ZOOMS.indexOf(z) + 1, ZOOMS.length - 1)]);
      if (e.key === "-") setZoom((z) => ZOOMS[Math.max(ZOOMS.indexOf(z) - 1, 0)]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!img) return null;
  return (
    <div className="viewer" role="dialog" aria-modal="true" aria-label={title} onClick={onClose}>
      <div className="viewer-panel" onClick={(e) => e.stopPropagation()}>
        <div className="viewer-bar">
          <b>{title}</b>
          {images.length > 1 && (
            <div className="seg">
              {images.map((im, k) => (
                <button key={im.key} className={k === i ? "on" : ""} onClick={() => setI(k)}>
                  {im.label}
                </button>
              ))}
            </div>
          )}
          <div className="seg">
            {ZOOMS.map((z) => (
              <button key={z} className={z === zoom ? "on" : ""} onClick={() => setZoom(z)}>
                {z}×
              </button>
            ))}
          </div>
          <span className="spacer" />
          <button className="btn ghost small" onClick={onClose} aria-label="Close viewer">
            Close (Esc)
          </button>
        </div>
        <div className="viewer-stage">
          <img
            src={img.src}
            alt={`${title}: ${img.label}`}
            style={{
              // 1x fits the whole image on any screen; larger zooms scroll.
              ...(zoom === 1
                ? { width: "100%", height: "min(72vh, 900px)", objectFit: "contain" as const }
                : { height: `calc(min(72vh, 900px) * ${zoom})`, width: "auto" }),
              filter: `brightness(${bright}%) contrast(${contrast}%)`,
            }}
          />
        </div>
        <div className="viewer-bar">
          <label className="slider">
            Brightness
            <input type="range" min={40} max={220} value={bright} onChange={(e) => setBright(+e.target.value)} />
          </label>
          <label className="slider">
            Contrast
            <input type="range" min={40} max={300} value={contrast} onChange={(e) => setContrast(+e.target.value)} />
          </label>
          <button className="btn ghost small" onClick={() => (setBright(100), setContrast(100), setZoom(1))}>
            Reset
          </button>
          <span className="spacer" />
          <span className="faint" style={{ fontSize: 11 }}>
            {img.note ?? "Public-dataset image, synthetic demo case."}
          </span>
        </div>
        {footer && <div className="viewer-bar">{footer}</div>}
      </div>
    </div>
  );
}
