# Design System Manifest

## 1. Visual Theme and Atmosphere (The "Why")
This application embraces a dual-personality aesthetic that feels simultaneously ultra-modern and timelessly organic. 
- **Dark Mode (Default):** Inspired by minimalist IDEs like Cursor. It is stealthy, precise, and highly technical. It uses deep, near-black tones, subtle borders, and stark contrast to minimize eye strain while keeping focus on the content.
- **Light Mode:** A departure from harsh, clinical whites. It embraces an organic, editorial feel using soft creams, warm beiges, and deep brown/black typography. It feels like high-quality matte paper.
- **The "Future-Ready" Vibe:** The UI is not static. It heavily incorporates spatial design, subtle 3D interactions, depth, and fluid physics to feel like a next-generation native application.

## 2. Color Palette and Semantic Tokens

### Dark Mode (Cursor-Minimalist)
| Role | Token (CSS Var) | Hex Value | Usage |
| :--- | :--- | :--- | :--- |
| **App Background** | `--bg-base` | `#0A0A0A` | The absolute bottom layer of the app. |
| **Surface** | `--bg-surface` | `#161616` | Cards, modals, sidebars. |
| **Borders** | `--border-subtle` | `#2A2A2A` | Dividers, card outlines (keep them 1px solid). |
| **Text Primary** | `--text-primary`| `#EDEDED` | Main headings and body text. |
| **Text Muted** | `--text-muted` | `#888888` | Secondary info, placeholders. |
| **Accent** | `--accent-main` | `#E0E0E0` | Hover states, active buttons (minimalist contrast). |

### Light Mode (Cream & Beige)
| Role | Token (CSS Var) | Hex Value | Usage |
| :--- | :--- | :--- | :--- |
| **App Background** | `--bg-base` | `#FDFBF7` | Alabaster cream for the main canvas. |
| **Surface** | `--bg-surface` | `#F2EBE1` | Warm beige for elevated elements. |
| **Borders** | `--border-subtle` | `#E2D9C8` | Soft, earthy borders. |
| **Text Primary** | `--text-primary`| `#2B2824` | Very dark, warm grey/brown (never pure black). |
| **Text Muted** | `--text-muted` | `#8C857B` | Secondary text. |
| **Accent** | `--accent-main` | `#1A1815` | High-contrast stark accent for primary buttons. |

## 3. Typography Rules
- **UI Font:** `Inter` or `Geist` (Clean, geometric, sans-serif).
- **Code/Monospace:** `JetBrains Mono` or `Geist Mono`.
- **Headings:** Tight tracking (letter-spacing: -0.03em), bold but not heavy (Weight: 500-600).
- **Body Text:** Loose, readable line-height (1.6), normal weight (400).

## 4. Layout and Components
- **Borders & Radii:** Use incredibly subtle `1px` borders to separate elements instead of heavy shadows. Border-radius should be sharp but forgiving (`6px` for small elements, `12px` for larger cards).
- **Glassmorphism (Subtle):** When navigation bars or overlays sit on top of content, use a backdrop blur (`backdrop-filter: blur(12px)`) with a slightly transparent surface color, rather than a solid block of color.
- **Buttons:** Padding should be generous (`10px 20px`). No heavy drop shadows. Rely on background color shifts on hover.

## 5. 🔮 Future-Ready & 3D Features (Crucial for AI Generation)
When prompted to build complex UI, dashboards, or hero sections, the AI must incorporate the following modern web techniques:

### A. Spatial Depth & 3D Card Physics
- Interactive cards should not just change color on hover. Use CSS `perspective`, `rotateX`, and `rotateY` to create a 3D tilt effect that tracks the user's mouse cursor. 
- Elements should feel like they exist in a 3D space, casting soft, dynamic, directional shadows based on cursor position.

### B. Canvas & WebGL Integration
- For "show-off" visual elements (like a hero background or a data visualization), default to suggesting or stubbing out `Three.js` or `@splinetool/react-spline` components.
- Examples include softly rotating 3D geometric primitives (torus, spheres) that use materials matching the current theme (e.g., a matte black wireframe in Dark Mode, a soft beige clay material in Light Mode).

### C. Fluid Motion & Micro-interactions
- **Easing:** Never use default `linear` or `ease-in-out` transitions. All animations must use custom spring physics or cubic-bezier curves (e.g., `transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1)`).
- **Mount Animations:** When a component enters the DOM, it should slightly scale up (`0.98` to `1`) and fade in (`opacity 0` to `1`) fluidly.
- **Grain/Noise:** In Dark Mode, apply a very subtle, animated SVG noise overlay to the background to give the UI a tactile, premium texture.