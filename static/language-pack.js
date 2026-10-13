/* Data-only language packs translate static text without modifying ledger data. */
(() => {
  const strings = window.accountingLanguageStrings || {};
  const tokens = window.accountingLanguageTokens || {};
  const normalise = (value) => value.replace(/\s+/g, " ").trim();
  const translate = (value) => {
    const exact = strings[value] || strings[normalise(value)];
    if (exact) return exact;
    // Translate longer names before shorter abbreviations.  This prevents
    // "Oct" from corrupting the word "October" in date labels.
    return Object.entries(tokens)
      .sort(([left], [right]) => right.length - left.length)
      .reduce((result, [from, to]) => result.replaceAll(from, to), value);
  };
  const skip = new Set(["SCRIPT", "STYLE", "TEXTAREA", "OPTION"]);

  document.addEventListener("DOMContentLoaded", () => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (!node.parentElement || skip.has(node.parentElement.tagName)) continue;
      const original = node.nodeValue;
      const translated = translate(original);
      if (translated !== original) node.nodeValue = translated;
    }
    for (const element of document.querySelectorAll("[placeholder], [title], input[type=submit], button")) {
      if (element.hasAttribute("placeholder")) element.setAttribute("placeholder", translate(element.getAttribute("placeholder")));
      if (element.hasAttribute("title")) element.setAttribute("title", translate(element.getAttribute("title")));
      if (element.tagName === "INPUT" && element.type === "submit") element.value = translate(element.value);
    }
    document.title = translate(document.title);
  });
})();
