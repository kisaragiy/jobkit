/* Navigation for reading only; an index is never promoted to a recipient identity. */
((expectedName, expectedSnapshot, rowIndex, observeInbox) => {
  const base={status:'conversation_changed',navigation_requested:false,recipient_verified:false,sending_enabled:false};
  const current=observeInbox(expectedName);
  if(current.status!=='observed_previews')return {...base,status:current.status};
  const fields=['display_name','party_summary','last_message_preview','preview_truncated','unread_marker','platform_conversation_id','identity_source'];
  if(current.rows.length!==expectedSnapshot.rows.length || current.list_truncated!==expectedSnapshot.list_truncated ||
     current.rows.some((row,i)=>fields.some(key=>row[key]!==expectedSnapshot.rows[i][key])))return base;
  if(!Number.isInteger(rowIndex)||rowIndex<0||rowIndex>=current.rows.length)return base;
  const target=current.rows[rowIndex];
  if(target.preview_truncated||!target.display_name||!target.party_summary)return {...base,status:'conversation_reference_incomplete'};
  if(current.rows.filter(row=>row.party_summary===target.party_summary&&row.last_message_preview===target.last_message_preview).length!==1)
    return {...base,status:'conversation_reference_ambiguous'};
  const rows=[...document.querySelector('.user-list-content').querySelectorAll('li')].filter(n=>n.getClientRects().length>0);
  const control=rows[rowIndex]?.querySelector('.friend-content');
  if(!control||!control.getClientRects().length)return {...base,status:'schema_changed'};
  control.click();
  return {...base,status:'navigation_requested',navigation_requested:true};
})
