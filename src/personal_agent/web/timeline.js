// Where question cards sit in the conversation.
// A question that is answered or no longer valid is history: it goes back to the
// moment it was asked. Only a question still waiting for the user stays below
// the latest message, where it can be answered.
export function placeQuestions(items, questions, render) {
  const closed = questions.filter(q => q.state !== 'pending').map(q => ({sort: q.created || 0, html: render(q)}));
  const history = [...items, ...closed].map((item, index) => ({...item, index}))
    .sort((a, b) => a.sort - b.sort || a.index - b.index).map(item => item.html);
  const pinned = questions.filter(q => q.state === 'pending')
    .sort((a, b) => (a.created || 0) - (b.created || 0)).map(render);
  return {history, pinned};
}
