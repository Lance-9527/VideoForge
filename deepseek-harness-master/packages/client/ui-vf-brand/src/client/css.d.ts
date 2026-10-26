/** CSS module names supplied by the client bundler. */
declare module '*.module.css' {
  const classes: Readonly<Record<string, string>>
  export default classes
}
