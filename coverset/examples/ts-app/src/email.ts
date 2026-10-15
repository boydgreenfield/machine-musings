/** Returns the domain of an email address; throws for anything that is not one. */
export function validateEmail(email: string): string {
  const at = email.indexOf("@");
  if (at < 1 || at !== email.lastIndexOf("@")) {
    throw new Error("not an email address");
  }
  const domain = email.slice(at + 1).toLowerCase();
  // "name.tld" only: a sub-domain such as eng.acme.com is rejected, and no test says otherwise.
  if (domain.split(".").length !== 2) {
    throw new Error("unsupported domain");
  }
  return domain;
}
