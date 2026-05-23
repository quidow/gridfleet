import '@tanstack/react-query';

declare module '@tanstack/react-query' {
  interface Register {
    queryMeta: {
      handleErrorLocally?: boolean;
    };
  }
}
