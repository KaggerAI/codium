type Ad = {
  title: string;
  url: string;
  imageUrl?: string;
  sponsor?: string;
};

const currentAd: Ad = {
  title: "Discover Kagger.ai — Intelligent Insights",
  sponsor: "Sponsored by Kagger.ai",
  url: "https://kagger.ai",
};

export function AdvertisementBanner() {
  return (
    <section aria-label="Advertisement" className="mt-12">
      <p className="text-xs uppercase tracking-[0.3em] text-muted-foreground text-center mb-3">
        Advertisement
      </p>
      <a
        href={currentAd.url}
        target="_blank"
        rel="noopener noreferrer sponsored"
        className="block rounded-2xl border-2 border-dashed border-[var(--gold)] bg-[var(--gradient-cosmos)] px-6 py-10 text-center text-primary-foreground hover:border-solid transition-all shadow-[var(--shadow-celestial)]"
      >
        <p className="text-xs uppercase tracking-widest text-[var(--gold)]">{currentAd.sponsor}</p>
        <p className="mt-2 font-display text-2xl md:text-3xl">{currentAd.title}</p>
        <p className="mt-2 text-sm text-primary-foreground/70">Click to learn more →</p>
      </a>
    </section>
  );
}
