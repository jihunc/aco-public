import type { Metadata } from 'next';
import { Providers } from './providers';
import './globals.css';
export const metadata: Metadata = {title: 'ACO · Collections workspace', description: 'Source-backed receivables and controlled collections'};
export default function RootLayout({children}: Readonly<{children: React.ReactNode}>) {
  return <html lang="en"><body><Providers>{children}</Providers></body></html>;
}
