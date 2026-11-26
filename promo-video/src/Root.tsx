import React from "react";
import { Composition } from "remotion";
import { PromoVideo } from "./PromoVideo";

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="LeadRadarPromo"
      component={PromoVideo}
      durationInFrames={180}
      fps={30}
      width={1280}
      height={720}
    />
  );
};
