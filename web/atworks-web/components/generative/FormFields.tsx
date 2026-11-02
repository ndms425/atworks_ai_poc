// Friendlier inputs for the question form: a 24-hour time picker made of taps (no AM/PM wheel), a
// number stepper with big buttons (no tiny spin arrows), and quick picks for dates.

"use client";

import { useState } from "react";

const CHIP_ON = "chip-selected";

const pad2 = (n: number) => String(n).padStart(2, "0");

function parseTime(value: string): { hour: number; minute: number } | null {
  const match = /^(\d{1,2}):(\d{2})/.exec(value.trim());
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  return hour < 24 && minute < 60 ? { hour, minute } : null;
}

const PRESET_TIMES = ["06:00", "09:00", "12:00", "18:00", "22:00", "00:00"];
const MINUTES = [0, 10, 15, 20, 30, 40, 45, 50];
/** The 24 hours in four six-hour rows, named the way people say them. */
const HOUR_BLOCKS: Array<[string, number]> = [["새벽", 0], ["오전", 6], ["오후", 12], ["저녁", 18]];

/** 24h "HH:MM". Common times are one tap; any other is an hour tap then a minute tap. */
export function TimeField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const parsed = parseTime(value);
  const [custom, setCustom] = useState(() => !!parsed && !PRESET_TIMES.includes(`${pad2(parsed.hour)}:${pad2(parsed.minute)}`));
  const current = parsed ? `${pad2(parsed.hour)}:${pad2(parsed.minute)}` : "";
  const set = (hour: number, minute: number) => onChange(`${pad2(hour)}:${pad2(minute)}`);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1">
        {PRESET_TIMES.map((time) => (
          <button
            key={time}
            type="button"
            className={`chip tabular-nums ${current === time ? CHIP_ON : ""}`}
            onClick={() => {
              onChange(time);
              setCustom(false);
            }}
          >
            {time}
          </button>
        ))}
        <button
          type="button"
          className="chip"
          aria-expanded={custom}
          onClick={() => setCustom((open) => !open)}
        >
          다른 시간 {custom ? "▴" : "▾"}
        </button>
        {current ? <span className="ml-auto text-[13px] font-semibold tabular-nums text-(--ink)">{current}</span> : null}
      </div>
      {custom ? (
        <div className="rounded-lg border border-(--line) bg-(--ground) p-2">
          <div className="mb-1 text-[11px] font-medium text-(--ink-soft)">시</div>
          <div className="flex flex-col gap-1">
            {HOUR_BLOCKS.map(([name, from]) => (
              <div key={name} className="grid grid-cols-[38px_repeat(6,minmax(0,1fr))] items-center gap-1">
                <span className="text-[11px] text-(--ink-soft)">{name}</span>
                {Array.from({ length: 6 }, (_, i) => from + i).map((hour) => (
                  <button
                    key={hour}
                    type="button"
                    onClick={() => set(hour, parsed?.minute ?? 0)}
                    className={`rounded-md py-1 text-[12.5px] tabular-nums transition-colors ${
                      parsed?.hour === hour ? "bg-(--accent) font-semibold text-(--on-accent)" : "bg-(--card) text-(--ink) hover:bg-(--accent-soft)"
                    }`}
                  >
                    {pad2(hour)}
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="mb-1 mt-2 text-[11px] font-medium text-(--ink-soft)">분</div>
          <div className="grid grid-cols-8 gap-1">
            {MINUTES.map((minute) => (
              <button
                key={minute}
                type="button"
                onClick={() => set(parsed?.hour ?? 9, minute)}
                className={`rounded-md py-1 text-[12.5px] tabular-nums transition-colors ${
                  parsed?.minute === minute ? "bg-(--accent) font-semibold text-(--on-accent)" : "bg-(--card) text-(--ink) hover:bg-(--accent-soft)"
                }`}
              >
                {pad2(minute)}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Quick picks for a count question, read from its label ("기간(일)" gets day counts). */
function presetsFor(label: string): number[] {
  if (/일|day/i.test(label)) return [1, 3, 5, 7, 14];
  if (/회|번|times|count/i.test(label)) return [1, 2, 3, 5, 10];
  return [];
}

/** Whole numbers with large − / + buttons and direct typing; never a spin arrow. */
export function NumberField({
  value,
  onChange,
  label,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
}) {
  const number = Number.parseInt(value, 10);
  const step = (delta: number) => onChange(String(Math.max(0, (Number.isFinite(number) ? number : 0) + delta)));
  const presets = presetsFor(label);
  const unit = /일|day/i.test(label) ? "일" : /회|번/.test(label) ? "회" : "";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-stretch overflow-hidden rounded-lg border border-(--line-strong) bg-(--card)">
        <button type="button" aria-label="줄이기" onClick={() => step(-1)} className="w-9 text-[18px] font-medium text-(--ink-soft) hover:bg-(--ground) hover:text-(--ink)">
          −
        </button>
        <input
          type="text"
          inputMode="numeric"
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value.replace(/[^\d]/g, ""))}
          aria-label={label}
          className="w-14 border-x border-(--line) bg-transparent py-1.5 text-center text-[14px] font-semibold tabular-nums text-(--ink) outline-none"
        />
        <button type="button" aria-label="늘리기" onClick={() => step(1)} className="w-9 text-[18px] font-medium text-(--ink-soft) hover:bg-(--ground) hover:text-(--ink)">
          +
        </button>
      </div>
      {unit ? <span className="text-[13px] text-(--ink-soft)">{unit}</span> : null}
      {presets.length ? (
        <div className="flex flex-wrap gap-1">
          {presets.map((n) => (
            <button key={n} type="button" className={`chip tabular-nums ${number === n ? CHIP_ON : ""}`} onClick={() => onChange(String(n))}>
              {n}
              {unit}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const isoDay = (date: Date) => `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;

/** The native date input plus the picks people actually use. */
export function DateField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const today = new Date();
  const plus = (days: number) => isoDay(new Date(today.getFullYear(), today.getMonth(), today.getDate() + days));
  const monday = (() => {
    const offset = (8 - today.getDay()) % 7 || 7;
    return plus(offset);
  })();
  const picks: Array<[string, string]> = [["오늘", plus(0)], ["내일", plus(1)], ["다음 주 월요일", monday]];
  return (
    <div className="flex flex-wrap items-center gap-1">
      {picks.map(([label, day]) => (
        <button key={label} type="button" className={`chip ${value === day ? CHIP_ON : ""}`} onClick={() => onChange(day)}>
          {label}
        </button>
      ))}
      <input
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="ml-auto rounded-lg border border-(--line-strong) bg-(--card) px-2 py-1 text-[13px] tabular-nums text-(--ink)"
      />
    </div>
  );
}

/** Target environments / servers are always a multi-pick, whatever type the model chose. */
export function isTargetQuestion(id: string, label: string): boolean {
  return /env|target|server/i.test(id) || /환경|서버|대상 계/.test(label);
}
