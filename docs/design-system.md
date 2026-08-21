# Clinetics Design System

## Brand Identity
Clinetics is an AI-powered healthcare operations platform. The design language is professional, clinical, and data-driven, prioritizing clarity, trust, and operational efficiency.

## Color Palette

### Surface & Backgrounds
*   **Surface**: `#f8f9ff` (Main application background)
*   **Surface Bright**: `#f8f9ff`
*   **Surface Dim**: `#cbdbf5`
*   **Container Lowest**: `#ffffff`
*   **Container Low**: `#eff4ff`
*   **Container**: `#e5e9f2`

### Brand & Accents
*   **Primary**: `#0d4a76` (Deep Clinical Navy)
*   **Primary Container**: `#d1e4ff`
*   **On-Primary Container**: `#001d36`
*   **Secondary**: `#535f70`
*   **Tertiary**: `#6b5778`

### Status & Utility
*   **Success**: `#4caf50` (Optimized/On Track)
*   **Warning**: `#ff9800` (Potential No-show)
*   **Error/Urgent**: `#ba1a1a` (Conflict/High Priority)
*   **Outline**: `#74777f`
*   **Outline Variant**: `#c4c6cf`

## Typography
*   **Primary Font**: Inter (Sans-serif)
*   **Headings**: Bold, Primary color, tight tracking.
*   **Data Labels**: Monospace or High-contrast Sans (for metrics).
*   **Body**: Regular weight, high readability on light surfaces.

## Design Tokens
*   **Roundness**: `ROUND_EIGHT` (8px border-radius for cards, buttons, and inputs).
*   **Shadows**: Subtle elevation (`shadow-sm`) for interactive elements; flat/bordered for data containers.
*   **Spacing**: 4px base grid (sm: 8px, md: 16px, lg: 24px, xl: 32px).

## Component Patterns

### Navigation
*   **Top Nav**: Global marketing/auth links. Primary Navy logo.
*   **Side Nav (Admin)**: 260px width, clear iconography, active state uses `Primary Container` background with bold text.

### Data Displays
*   **Metric Cards**: Large bold values, trend indicators (% change), and clear labels.
*   **Charts**: Use refined, clinical colors (Navy, Teal, Slate). Avoid overly vibrant "neon" palettes.
*   **Optimization Buffers**: Distinctive dashed borders or specialized "sparkle" iconography to denote AI-generated slots.

### Agent Interface
*   **Chat Sidebar**: Dedicated vertical container (right side) for natural language interaction.
*   **Message Bubbles**: Clean, bordered bubbles. Grounded citations linked to chart data.
