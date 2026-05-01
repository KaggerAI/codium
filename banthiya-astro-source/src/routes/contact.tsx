import { createFileRoute } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { SiteFooter } from "@/components/SiteFooter";
import { Mail, Phone } from "lucide-react";

export const Route = createFileRoute("/contact")({
  head: () => ({
    meta: [
      { title: "Contact Us — Banthiya Astro Predictions" },
      { name: "description", content: "Get in touch with Banthiya Astro Predictions for subscriptions and inquiries." },
      { property: "og:title", content: "Contact Us — Banthiya Astro Predictions" },
      { property: "og:description", content: "Reach out for subscriptions and inquiries." },
    ],
  }),
  component: Contact,
});

function Contact() {
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <SiteHeader />
      <main className="flex-1 mx-auto w-full max-w-3xl px-6 py-16">
        <h2 className="text-4xl font-semibold text-center">Contact Us</h2>
        <p className="mt-3 text-center text-muted-foreground">
          We'd love to hear from you. Reach out using the details below.
        </p>
        <div className="mt-10 grid gap-4 sm:grid-cols-2">
          <a href="mailto:contact@banthiyaastro.com" className="flex items-center gap-3 rounded-2xl border-2 border-border p-6 hover:border-[var(--gold)] transition-colors">
            <Mail className="h-6 w-6 text-[var(--gold)]" />
            <div>
              <p className="text-xs uppercase tracking-widest text-muted-foreground">Email</p>
              <p className="font-medium">contact@banthiyaastro.com</p>
            </div>
          </a>
          <a href="tel:+910000000000" className="flex items-center gap-3 rounded-2xl border-2 border-border p-6 hover:border-[var(--gold)] transition-colors">
            <Phone className="h-6 w-6 text-[var(--gold)]" />
            <div>
              <p className="text-xs uppercase tracking-widest text-muted-foreground">Phone</p>
              <p className="font-medium">+91 00000 00000</p>
            </div>
          </a>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
