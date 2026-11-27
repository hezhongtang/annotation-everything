"use strict";

function selectedPassage(api, xml, changes) {
  // The saved selection XML can corroborate an adjacent add/delete when Current includes the deletion.
  const agrees = xml && xml.original === api.original &&
    (xml.current === api.current || xml.current === api.text ||
      (changes.length && xml.revisions.length === changes.length && changes.every(change =>
        xml.revisions.some(r => r.type === change.type && r.author === change.author))));
  const original = api.original;
  const current = agrees ? xml.current : api.current;
  const revisions = changes.map(change => {
    const added = change.type === "Added", deleted = change.type === "Deleted";
    const view = added ? current : original;
    const reviewed = added ? change.range.current : change.range.original;
    const candidates = agrees ? xml.revisions.filter(r => r.type === change.type && r.author === change.author) : [];
    let text = "";
    if ((added || deleted) && change.inside) {
      if (reviewed && view.includes(reviewed) && (!change.text || change.text === reviewed) &&
          (!candidates.length || candidates.some(r => r.text === reviewed))) text = reviewed;
      else if (candidates.length === 1 && candidates[0].text && view.includes(candidates[0].text) &&
               changes.filter(c => c.type === change.type && c.author === change.author).length === 1)
        text = candidates[0].text;
    }
    return {type: change.type, author: change.author, date: change.date, text, verified: Boolean(text)};
  });
  return {original, current, revisions};
}

if (typeof module !== "undefined" && module.exports) module.exports = selectedPassage;
