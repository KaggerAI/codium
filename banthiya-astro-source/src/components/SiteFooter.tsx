export function SiteFooter() {
  return (
    <footer className="border-t border-border bg-primary text-primary-foreground">
      <div className="mx-auto max-w-6xl px-6 py-8 text-center text-sm">
        <p className="font-display text-lg">Banthiya Astro Predictions</p>
        <p className="mt-2 text-primary-foreground/70">
          © {new Date().getFullYear()} All rights reserved. Predictions are for guidance and educational purposes.
        </p>
      </div>
    </footer>
  );
}
