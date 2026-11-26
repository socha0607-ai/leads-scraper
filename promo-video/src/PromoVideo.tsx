import React from "react";
import {
  AbsoluteFill,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const CREAM = "#F7F5EE";
const LIME = "#D4FF00";
const COBALT = "#2563EB";
const BLACK = "#111111";
const RED = "#EF4444";
const GREEN = "#22C55E";
const WHITE = "#FFFFFF";

const FONT_FAMILY =
  '"Inter", "Arial Black", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

const SPRING_CFG = { damping: 12, stiffness: 180 };

// ---------------------------------------------------------------------------
// Scene 1 (0.0s - 1.5s | Frames 0 - 45): Kinetic Typography Hook
// ---------------------------------------------------------------------------
const SceneHook: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const stopSpring = spring({ frame, fps, config: SPRING_CFG });
  const coldSpring = spring({ frame: Math.max(0, frame - 8), fps, config: SPRING_CFG });
  const emailSpring = spring({ frame: Math.max(0, frame - 16), fps, config: SPRING_CFG });

  const slashProgress = interpolate(frame, [26, 36], [0, 100], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const camZoom = interpolate(frame, [0, 45], [1.0, 1.12], {
    extrapolateRight: "clamp",
  });

  const whipOut = interpolate(frame, [39, 45], [0, -1400], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        backgroundColor: CREAM,
        justifyContent: "center",
        alignItems: "center",
        fontFamily: FONT_FAMILY,
        transform: `translateX(${whipOut}px) scale(${camZoom})`,
      }}
    >
      {/* Subtle Neo-Brutalist Grid */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          backgroundImage:
            "radial-gradient(rgba(17,17,17,0.12) 2px, transparent 2px)",
          backgroundSize: "32px 32px",
        }}
      />

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 10,
          position: "relative",
        }}
      >
        <div
          style={{
            fontSize: 122,
            fontWeight: 900,
            color: BLACK,
            letterSpacing: "-0.04em",
            lineHeight: 0.95,
            transform: `scale(${interpolate(stopSpring, [0, 1], [0.4, 1])})`,
            opacity: stopSpring,
          }}
        >
          STOP
        </div>

        <div
          style={{
            fontSize: 122,
            fontWeight: 900,
            color: COBALT,
            letterSpacing: "-0.04em",
            lineHeight: 0.95,
            transform: `scale(${interpolate(coldSpring, [0, 1], [0.4, 1])})`,
            opacity: coldSpring,
          }}
        >
          COLD
        </div>

        <div
          style={{
            position: "relative",
            fontSize: 122,
            fontWeight: 900,
            color: BLACK,
            letterSpacing: "-0.04em",
            lineHeight: 0.95,
            transform: `scale(${interpolate(emailSpring, [0, 1], [0.4, 1])})`,
            opacity: emailSpring,
          }}
        >
          EMAILING.
          {/* Bright Red Strikethrough Slash */}
          <div
            style={{
              position: "absolute",
              left: "-4%",
              top: "52%",
              height: 18,
              width: `${slashProgress * 1.08}%`,
              backgroundColor: RED,
              border: `3px solid ${BLACK}`,
              boxShadow: `4px 4px 0px ${BLACK}`,
              transform: "translateY(-50%) rotate(-3deg)",
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------------------
// Scene 2 (1.5s - 3.5s | Frames 45 - 105): Radar & Platform Pop
// ---------------------------------------------------------------------------
const SceneRadar: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const whipIn = spring({ frame, fps, config: { damping: 14, stiffness: 200 } });
  const slideX = interpolate(whipIn, [0, 1], [1280, 0]);

  const redditPop = spring({ frame: Math.max(0, frame - 6), fps, config: SPRING_CFG });
  const hnPop = spring({ frame: Math.max(0, frame - 13), fps, config: SPRING_CFG });
  const xPop = spring({ frame: Math.max(0, frame - 20), fps, config: SPRING_CFG });
  const detectPop = spring({ frame: Math.max(0, frame - 30), fps, config: SPRING_CFG });

  const camZoom = interpolate(frame, [0, 60], [1.0, 1.1], {
    extrapolateRight: "clamp",
  });

  const pulseScale = 1 + Math.sin(frame * 0.35) * 0.08;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: CREAM,
        justifyContent: "center",
        alignItems: "center",
        fontFamily: FONT_FAMILY,
        transform: `translateX(${slideX}px) scale(${camZoom})`,
      }}
    >
      {/* Concentric Radar Rings */}
      {[320, 480, 640].map((size, idx) => (
        <div
          key={size}
          style={{
            position: "absolute",
            width: size,
            height: size,
            borderRadius: "50%",
            border: `3px dashed ${idx === 1 ? COBALT : "rgba(17,17,17,0.22)"}`,
            transform: `scale(${pulseScale}) rotate(${frame * (idx % 2 === 0 ? 1.5 : -1.2)}deg)`,
          }}
        />
      ))}

      {/* Floating 3D-Style Platform Pill Badges */}
      <div
        style={{
          display: "flex",
          gap: 28,
          alignItems: "center",
          marginBottom: 42,
          zIndex: 2,
        }}
      >
        {[
          { label: "Reddit", dot: "#FF4500", pop: redditPop, rot: -4 },
          { label: "Hacker News", dot: "#FF6600", pop: hnPop, rot: 2 },
          { label: "𝕏 / Twitter", dot: BLACK, pop: xPop, rot: -3 },
        ].map((item) => (
          <div
            key={item.label}
            style={{
              backgroundColor: WHITE,
              border: `4px solid ${BLACK}`,
              boxShadow: `6px 6px 0px ${BLACK}`,
              borderRadius: 999,
              padding: "18px 34px",
              display: "flex",
              alignItems: "center",
              gap: 14,
              fontSize: 34,
              fontWeight: 900,
              color: BLACK,
              transform: `scale(${item.pop}) rotate(${item.rot}deg)`,
              opacity: item.pop,
            }}
          >
            <div
              style={{
                width: 22,
                height: 22,
                borderRadius: "50%",
                backgroundColor: item.dot,
                border: `2px solid ${BLACK}`,
              }}
            />
            {item.label}
          </div>
        ))}
      </div>

      {/* Live Scanner Badge */}
      <div
        style={{
          zIndex: 3,
          backgroundColor: LIME,
          color: BLACK,
          border: `4px solid ${BLACK}`,
          boxShadow: `8px 8px 0px ${BLACK}`,
          borderRadius: 18,
          padding: "20px 44px",
          fontSize: 44,
          fontWeight: 900,
          letterSpacing: "-0.02em",
          transform: `scale(${interpolate(detectPop, [0, 1], [0.5, 1])})`,
          opacity: detectPop,
        }}
      >
        ⚡ AI BUYER DETECTED
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------------------
// Scene 3 (3.5s - 5.0s | Frames 105 - 150): The Money Shot — Lead Card Zoom
// ---------------------------------------------------------------------------
const SceneLeadCard: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const cardPop = spring({ frame, fps, config: SPRING_CFG });
  const camZoom = interpolate(frame, [0, 45], [1.0, 1.12], {
    extrapolateRight: "clamp",
  });

  // Cursor flies in frames 10..24, clicks at frame 26
  const cursorMove = spring({
    frame: Math.max(0, frame - 8),
    fps,
    config: { damping: 14, stiffness: 190 },
  });
  const cursorX = interpolate(cursorMove, [0, 1], [360, 195]);
  const cursorY = interpolate(cursorMove, [0, 1], [230, 108]);

  const isClicked = frame >= 25;
  const clickBounce = isClicked
    ? spring({ frame: frame - 25, fps, config: { damping: 10, stiffness: 240 } })
    : 0;
  const btnScale = isClicked
    ? interpolate(clickBounce, [0, 0.5, 1], [0.9, 1.08, 1])
    : 1;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: COBALT,
        justifyContent: "center",
        alignItems: "center",
        fontFamily: FONT_FAMILY,
        transform: `scale(${camZoom})`,
      }}
    >
      {/* Subtle diagonal stripes */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          backgroundImage:
            "repeating-linear-gradient(-45deg, rgba(255,255,255,0.06) 0px, rgba(255,255,255,0.06) 14px, transparent 14px, transparent 28px)",
        }}
      />

      {/* Telegram Notification Card */}
      <div
        style={{
          width: 780,
          backgroundColor: CREAM,
          border: `5px solid ${BLACK}`,
          boxShadow: `12px 12px 0px ${BLACK}`,
          borderRadius: 24,
          padding: "36px 40px",
          display: "flex",
          flexDirection: "column",
          gap: 22,
          position: "relative",
          transform: `scale(${interpolate(cardPop, [0, 1], [0.65, 1])})`,
          opacity: cardPop,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div
            style={{
              backgroundColor: LIME,
              color: BLACK,
              border: `3px solid ${BLACK}`,
              boxShadow: `4px 4px 0px ${BLACK}`,
              borderRadius: 999,
              padding: "10px 22px",
              fontSize: 26,
              fontWeight: 900,
            }}
          >
            🔥 INTENT SCORE: 9/10
          </div>
          <div style={{ fontSize: 22, fontWeight: 800, color: COBALT }}>
            r/startups • 2m ago
          </div>
        </div>

        <div
          style={{
            backgroundColor: WHITE,
            border: `4px solid ${BLACK}`,
            boxShadow: `5px 5px 0px ${BLACK}`,
            borderRadius: 16,
            padding: "24px 28px",
            fontSize: 38,
            fontWeight: 900,
            color: BLACK,
            lineHeight: 1.2,
          }}
        >
          “Need Next.js Dev for MVP ($4,000)”
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <div
            style={{
              backgroundColor: isClicked ? GREEN : BLACK,
              color: isClicked ? BLACK : WHITE,
              border: `4px solid ${BLACK}`,
              boxShadow: `5px 5px 0px ${BLACK}`,
              borderRadius: 14,
              padding: "16px 32px",
              fontSize: 28,
              fontWeight: 900,
              transform: `scale(${btnScale})`,
            }}
          >
            {isClicked ? "✅ SENT FIRST!" : "📋 COPY PITCH"}
          </div>
        </div>

        {/* Animated Cursor */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: "50%",
            transform: `translate(${cursorX}px, ${cursorY}px) scale(${frame >= 24 && frame <= 28 ? 0.82 : 1})`,
            opacity: cursorMove,
            pointerEvents: "none",
            zIndex: 10,
          }}
        >
          <svg width="46" height="46" viewBox="0 0 24 24" fill="none">
            <path
              d="M5 3L19 12L12.5 13.8L9.5 20.5L5 3Z"
              fill={WHITE}
              stroke={BLACK}
              strokeWidth="2"
              strokeLinejoin="round"
            />
          </svg>
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------------------
// Scene 4 (5.0s - 6.0s | Frames 150 - 180): CTA End Card
// ---------------------------------------------------------------------------
const SceneCTA: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const headlinePop = spring({ frame, fps, config: SPRING_CFG });
  const subPop = spring({ frame: Math.max(0, frame - 6), fps, config: SPRING_CFG });
  const camZoom = interpolate(frame, [0, 30], [1.0, 1.08], {
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        backgroundColor: LIME,
        justifyContent: "center",
        alignItems: "center",
        fontFamily: FONT_FAMILY,
        transform: `scale(${camZoom})`,
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 28,
        }}
      >
        <div
          style={{
            fontSize: 96,
            fontWeight: 900,
            color: BLACK,
            letterSpacing: "-0.04em",
            textAlign: "center",
            transform: `scale(${interpolate(headlinePop, [0, 1], [0.5, 1])})`,
            opacity: headlinePop,
          }}
        >
          5-DAY FREE TRIAL
        </div>

        <div
          style={{
            backgroundColor: WHITE,
            color: BLACK,
            border: `5px solid ${BLACK}`,
            boxShadow: `10px 10px 0px ${BLACK}`,
            borderRadius: 999,
            padding: "22px 48px",
            fontSize: 44,
            fontWeight: 900,
            transform: `scale(${interpolate(subPop, [0, 1], [0.5, 1])})`,
            opacity: subPop,
          }}
        >
          👉 @Lead_Scraper01_bot
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------------------
// Main 6-Second Composition (180 Frames @ 30fps)
// ---------------------------------------------------------------------------
export const PromoVideo: React.FC = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: CREAM }}>
      <Sequence from={0} durationInFrames={45}>
        <SceneHook />
      </Sequence>
      <Sequence from={45} durationInFrames={60}>
        <SceneRadar />
      </Sequence>
      <Sequence from={105} durationInFrames={45}>
        <SceneLeadCard />
      </Sequence>
      <Sequence from={150} durationInFrames={30}>
        <SceneCTA />
      </Sequence>
    </AbsoluteFill>
  );
};
