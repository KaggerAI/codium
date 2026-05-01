import { Link } from "@tanstack/react-router";

const navItems = [
  { to: "/", label: "Home" },
  { to: "/about", label: "About Us" },
  { to: "/subscriptions", label: "Subscription Plans" },
  { to: "/contact", label: "Contact Us" },
] as const;

export function SiteHeader() {
  return (
    <header className="border-b border-border bg-[var(--gradient-cosmos)] text-primary-foreground">
      <div className="mx-auto max-w-6xl px-6 py-8 text-center">
        <p className="text-xs uppercase tracking-[0.4em] text-[var(--gold)]">Vedic Mundane Astrology</p>
        <h1 className="mt-2 text-4xl md:text-5xl font-semibold text-[var(--gold)]">
          Banthiya Astro Predictions
        </h1>
      </div>
      <nav className="border-t border-white/10 bg-black/20">
        <ul className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-2 px-6 py-3 text-sm">
          {navItems.map((item) => (
            <li key={item.to}>
              <Link
                to={item.to}
                activeOptions={{ exact: true }}
                activeProps={{ className: "text-[var(--gold)]" }}
                className="px-4 py-2 uppercase tracking-widest text-primary-foreground/80 hover:text-[var(--gold)] transition-colors"
              >
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}
