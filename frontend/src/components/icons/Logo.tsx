const logoSrc = `${import.meta.env.BASE_URL}brand-mark.svg`;

export default function Logo({ className }: { className?: string }) {
  return <img src={logoSrc} className={className} alt="" aria-hidden="true" draggable={false} />;
}