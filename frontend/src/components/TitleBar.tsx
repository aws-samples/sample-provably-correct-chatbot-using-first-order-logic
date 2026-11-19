import React from 'react';

interface TitleBarProps {
  title: string;
}

export function TitleBar({ title }: TitleBarProps) {
  return (
    <header className="title-bar" role="banner">
      {title}
    </header>
  );
}
