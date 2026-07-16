import { useState } from "react";
import { isDisconnected, usePoll } from "./data/poll";
import { isErr } from "./data/types";
import ActivityFeed from "./components/ActivityFeed";
import AnomaliesPanel from "./components/AnomaliesPanel";
import { AlertBanner, DisconnectedBanner } from "./components/Banners";
import FillsPanel from "./components/FillsPanel";
import FunnelPanel from "./components/FunnelPanel";
import HeaderBar from "./components/HeaderBar";
import LogsPanel from "./components/LogsPanel";
import NowStrip from "./components/NowStrip";
import OvernightPanel from "./components/OvernightPanel";
import PositionsPanel from "./components/PositionsPanel";
import RunsPanel from "./components/RunsPanel";
import SleeveCards from "./components/SleeveCards";
import SleeveDrillDrawer from "./components/SleeveDrillDrawer";

export default function App() {
  const poll = usePoll();
  const [drill, setDrill] = useState<string | null>(null);
  const snap = poll.snapshot;
  const health = snap && !isErr(snap.health) ? snap.health : null;
  const sleeves = snap?.sleeves ?? null;
  const drillSleeve = drill && sleeves && !isErr(sleeves) ? sleeves[drill] : undefined;

  return (
    <>
      {isDisconnected(poll) && <DisconnectedBanner lastGoodAt={poll.lastGoodAt} />}
      <HeaderBar health={snap?.health ?? null} market={snap?.market ?? null}
        lastGoodAt={poll.lastGoodAt} />
      <AlertBanner health={health} />
      <NowStrip activity={snap?.activity ?? null} health={snap?.health ?? null} />
      <div className="wrap">
        <SleeveCards sleeves={sleeves} onOpen={setDrill} />
        <div className="cols">
          <div className="col">
            <PositionsPanel sleeves={sleeves} />
            <FillsPanel sleeves={sleeves} />
            <FunnelPanel funnel={snap?.funnel ?? null} />
          </div>
          <div className="col">
            <ActivityFeed events={poll.events} />
            <OvernightPanel funnel={snap?.funnel ?? null} />
            <RunsPanel activity={snap?.activity ?? null} />
            <AnomaliesPanel anomalies={snap?.anomalies_7d ?? null} />
            <LogsPanel />
          </div>
        </div>
      </div>
      {drill && (
        <SleeveDrillDrawer name={drill} sleeve={drillSleeve}
          onClose={() => setDrill(null)} />
      )}
    </>
  );
}
